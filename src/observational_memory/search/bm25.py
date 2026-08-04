"""Fixed BM25 authority backed by immutable canonical-JSON generations."""

from __future__ import annotations

import errno
import json
import os
import pickle
import re
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # Windows does not provide POSIX getrusage.
    resource = None  # type: ignore[assignment]

from . import Document, SearchResult
from .generation import (
    DOCUMENT_SCHEMA_VERSION,
    INDEX_SCHEMA_VERSION,
    MATERIALIZATION_SCHEMA_VERSION,
    DocumentBatch,
    canonical_json_bytes,
    document_from_payload,
    document_payload,
    sha256_hex,
)
from .generation_store import (
    GenerationStoreError,
    StoreReadRoot,
    StoreTransaction,
    _duplicate_root_fd,
    _open_dir,
    _PointerChanged,
    _read_file,
    _require_regular,
    _require_store_transaction,
    _secure_open_root,
    _try_generation_lock,
    _unlock,
    _validate_canonical_read_root,
    _write_file,
    validate_canonical_root,
)

CURRENT_POINTER_SCHEMA = "om.search.current-generation.v1"
GENERATION_MANIFEST_SCHEMA = "om.search.generation-manifest.v1"
BM25_INDEX_SCHEMA = "om.search.bm25-index.v3"
BM25_BACKEND_CONFIG = {
    "name": "bm25",
    "rank_bm25": "BM25Okapi",
    "tokenizer": "om-bm25-v1",
}
BM25_BACKEND_CONFIG_DIGEST = sha256_hex(canonical_json_bytes(BM25_BACKEND_CONFIG))
BRIDGE_GENERATION_SCHEMA = "om.native-memory-bridge.generation.v2"

# The current generation plus three rollback candidates. Reader-pinned
# generations can temporarily exceed this bound and are retried on the next
# publication; a live reader is never invalidated to satisfy retention.
MAX_RETAINED_GENERATIONS = 4

_GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_RETIRED_GENERATION = re.compile(r"^\.retired\.[0-9a-f]{64}\.[0-9a-f]{32}$")
_BRIDGE_METADATA_KEYS = {
    "schema",
    "desired_state_digest",
    "policy_digest",
    "document_bytes_sha256",
    "sources",
}
_INDEX_KEYS = {
    "schema",
    "generation_id",
    "backend_name",
    "backend_config_digest",
    "document_schema_version",
    "materialization_schema_version",
    "index_schema_version",
    "content_digest",
    "documents",
    "tokenized_corpus",
}
_MANIFEST_BASE_KEYS = {
    "schema",
    "generation_id",
    "backend_name",
    "backend_config_digest",
    "document_schema_version",
    "materialization_schema_version",
    "index_schema_version",
    "document_count",
    "content_digest",
    "canonical_corpus_sha256",
    "index_sha256",
}
_MANIFEST_BRIDGE_KEYS = {
    "bridge_schema",
    "desired_state_digest",
    "policy_digest",
    "document_bytes_sha256",
    "sources",
}

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "but",
        "not",
        "with",
        "by",
        "from",
    }
)


@dataclass(frozen=True)
class VerifiedBM25Generation:
    generation_id: str
    index_bytes: bytes
    manifest_bytes: bytes
    index: dict[str, Any]
    manifest: dict[str, Any]


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip markdown/emoji, remove stopwords."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [word for word in text.split() if word and word not in _STOPWORDS]


def _parse_canonical_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GenerationStoreError(f"BM25 {label} is malformed") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
        raise GenerationStoreError(f"BM25 {label} is not canonical JSON")
    return value


def _validate_batch(batch: DocumentBatch) -> None:
    if not isinstance(batch, DocumentBatch):
        raise TypeError("BM25 publication requires one immutable DocumentBatch")
    if batch.backend_name != "bm25":
        raise ValueError("BM25 store cannot publish a different backend")
    if batch.backend_config_digest != BM25_BACKEND_CONFIG_DIGEST:
        raise ValueError("BM25 batch has an unsupported backend configuration")
    if batch.document_schema_version != DOCUMENT_SCHEMA_VERSION:
        raise ValueError("BM25 batch has an unsupported document schema")
    if batch.materialization_schema_version != MATERIALIZATION_SCHEMA_VERSION:
        raise ValueError("BM25 batch has an unsupported materialization schema")
    if batch.index_schema_version != INDEX_SCHEMA_VERSION:
        raise ValueError("BM25 batch has an unsupported index schema")
    if _GENERATION_ID.fullmatch(batch.generation_id) is None:
        raise ValueError("BM25 batch generation ID is not content-addressed")


def _validated_bridge_metadata(bridge_metadata: dict[str, Any] | None) -> dict[str, Any]:
    if bridge_metadata is None:
        return {}
    if not isinstance(bridge_metadata, dict) or set(bridge_metadata) != _BRIDGE_METADATA_KEYS:
        raise ValueError("BM25 bridge metadata has an unsupported shape")
    if bridge_metadata.get("schema") != BRIDGE_GENERATION_SCHEMA:
        raise ValueError("BM25 bridge metadata schema is invalid")
    for key in ("desired_state_digest", "policy_digest", "document_bytes_sha256"):
        value = bridge_metadata.get(key)
        if not isinstance(value, str) or _GENERATION_ID.fullmatch(value) is None:
            raise ValueError(f"BM25 bridge metadata {key} is invalid")
    sources = bridge_metadata.get("sources")
    if not isinstance(sources, list):
        raise ValueError("BM25 bridge metadata sources are invalid")
    normalized_sources: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source_id", "content_hash"}:
            raise ValueError("BM25 bridge source metadata is invalid")
        source_id = source.get("source_id")
        content_hash = source.get("content_hash")
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(content_hash, str)
            or _GENERATION_ID.fullmatch(content_hash) is None
        ):
            raise ValueError("BM25 bridge source metadata is invalid")
        normalized_sources.append({"source_id": source_id, "content_hash": content_hash})
    return {
        "bridge_schema": BRIDGE_GENERATION_SCHEMA,
        "desired_state_digest": bridge_metadata["desired_state_digest"],
        "policy_digest": bridge_metadata["policy_digest"],
        "document_bytes_sha256": bridge_metadata["document_bytes_sha256"],
        "sources": normalized_sources,
    }


def _verify_bm25_generation(index_bytes: bytes, manifest_bytes: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the one non-substitutable BM25 persisted-byte verification policy."""
    index = _parse_canonical_object(index_bytes, "index")
    manifest = _parse_canonical_object(manifest_bytes, "manifest")
    if set(index) != _INDEX_KEYS:
        raise GenerationStoreError("BM25 index has unsupported or incomplete claims")
    if index.get("schema") != BM25_INDEX_SCHEMA:
        raise GenerationStoreError("BM25 index schema is invalid")
    if manifest.get("schema") != GENERATION_MANIFEST_SCHEMA:
        raise GenerationStoreError("BM25 generation manifest schema is invalid")
    generation_id = index.get("generation_id")
    if not isinstance(generation_id, str) or manifest.get("generation_id") != generation_id:
        raise GenerationStoreError("BM25 index and manifest generation IDs disagree")
    for key in (
        "backend_name",
        "backend_config_digest",
        "document_schema_version",
        "materialization_schema_version",
        "index_schema_version",
        "content_digest",
    ):
        if index.get(key) != manifest.get(key):
            raise GenerationStoreError(f"BM25 index and manifest disagree on {key}")
    if index.get("backend_name") != "bm25":
        raise GenerationStoreError("generation is not a local BM25 index")
    if index.get("backend_config_digest") != BM25_BACKEND_CONFIG_DIGEST:
        raise GenerationStoreError("BM25 backend configuration digest is invalid")
    if index.get("document_schema_version") != DOCUMENT_SCHEMA_VERSION:
        raise GenerationStoreError("BM25 document schema is invalid")
    if index.get("materialization_schema_version") != MATERIALIZATION_SCHEMA_VERSION:
        raise GenerationStoreError("BM25 materialization schema is invalid")
    if index.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        raise GenerationStoreError("BM25 logical index schema is invalid")
    document_values = index.get("documents")
    tokenized = index.get("tokenized_corpus")
    if not isinstance(document_values, list) or not isinstance(tokenized, list):
        raise GenerationStoreError("BM25 index corpus is invalid")
    try:
        documents = tuple(document_from_payload(value) for value in document_values)
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationStoreError("BM25 index document payload is invalid") from exc
    if tokenized != [_tokenize(document.content) for document in documents]:
        raise GenerationStoreError("BM25 persisted tokens do not match its documents")
    try:
        batch = DocumentBatch.create(
            documents,
            backend_name="bm25",
            backend_config_digest=BM25_BACKEND_CONFIG_DIGEST,
            generation_id=generation_id,
            document_schema_version=DOCUMENT_SCHEMA_VERSION,
            materialization_schema_version=MATERIALIZATION_SCHEMA_VERSION,
            index_schema_version=INDEX_SCHEMA_VERSION,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationStoreError("BM25 persisted corpus cannot form one immutable batch") from exc
    if batch.content_digest != index.get("content_digest"):
        raise GenerationStoreError("BM25 canonical corpus digest is invalid")
    if sha256_hex(batch.canonical_corpus_bytes) != manifest.get("canonical_corpus_sha256"):
        raise GenerationStoreError("BM25 manifest corpus-byte digest is invalid")
    if len(documents) != manifest.get("document_count"):
        raise GenerationStoreError("BM25 document count is invalid")
    if sha256_hex(index_bytes) != manifest.get("index_sha256"):
        raise GenerationStoreError("BM25 persisted index-byte digest is invalid")
    identity_documents = []
    for value in document_values:
        if not isinstance(value, dict):
            raise GenerationStoreError("BM25 index document payload is invalid")
        identity_value = dict(value)
        metadata = dict(identity_value.get("metadata") or {})
        metadata.pop("generation_id", None)
        identity_value["metadata"] = metadata
        identity_documents.append(identity_value)
    if manifest.get("bridge_schema") == BRIDGE_GENERATION_SCHEMA:
        if set(manifest) != _MANIFEST_BASE_KEYS | _MANIFEST_BRIDGE_KEYS:
            raise GenerationStoreError("BM25 bridge generation manifest has unsupported claims")
        sources = manifest.get("sources")
        if not isinstance(sources, list) or any(
            not isinstance(source, dict)
            or set(source) != {"source_id", "content_hash"}
            or not isinstance(source.get("source_id"), str)
            or not isinstance(source.get("content_hash"), str)
            or _GENERATION_ID.fullmatch(source["content_hash"]) is None
            for source in sources
        ):
            raise GenerationStoreError("bridge generation source manifest is invalid")
        document_bytes = canonical_json_bytes(identity_documents)
        if sha256_hex(document_bytes) != manifest.get("document_bytes_sha256"):
            raise GenerationStoreError("bridge generation document-byte digest is invalid")
        identity = {
            "schema": BRIDGE_GENERATION_SCHEMA,
            "desired_state_digest": manifest.get("desired_state_digest"),
            "policy_digest": manifest.get("policy_digest"),
            "sources": [(source["source_id"], source["content_hash"]) for source in sources],
            "document_schema_version": DOCUMENT_SCHEMA_VERSION,
            "materialization_schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "backend_name": "bm25",
            "backend_config_digest": BM25_BACKEND_CONFIG_DIGEST,
            "document_bytes_sha256": manifest.get("document_bytes_sha256"),
        }
    elif any(
        key in manifest
        for key in (
            "bridge_schema",
            "desired_state_digest",
            "policy_digest",
            "document_bytes_sha256",
            "sources",
        )
    ):
        raise GenerationStoreError("BM25 manifest contains partial bridge metadata")
    else:
        if set(manifest) != _MANIFEST_BASE_KEYS:
            raise GenerationStoreError("BM25 generation manifest has unsupported or incomplete claims")
        identity = {
            "document_schema_version": DOCUMENT_SCHEMA_VERSION,
            "materialization_schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "backend_name": "bm25",
            "backend_config_digest": BM25_BACKEND_CONFIG_DIGEST,
            "identity": {},
            "documents": identity_documents,
        }
    if sha256_hex(canonical_json_bytes(identity)) != generation_id:
        raise GenerationStoreError("generation ID is not the content address of persisted claims")
    return index, manifest


def _build_bm25_generation(
    batch: DocumentBatch,
    *,
    bridge_metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_batch(batch)
    documents = tuple(batch.documents)
    index = {
        "schema": BM25_INDEX_SCHEMA,
        "generation_id": batch.generation_id,
        "backend_name": "bm25",
        "backend_config_digest": BM25_BACKEND_CONFIG_DIGEST,
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "materialization_schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "content_digest": batch.content_digest,
        "documents": [document_payload(document) for document in documents],
        "tokenized_corpus": [_tokenize(document.content) for document in documents],
    }
    index_bytes = canonical_json_bytes(index) + b"\n"
    manifest = {
        "schema": GENERATION_MANIFEST_SCHEMA,
        "generation_id": batch.generation_id,
        "backend_name": "bm25",
        "backend_config_digest": BM25_BACKEND_CONFIG_DIGEST,
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "materialization_schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "document_count": len(batch.document_bytes),
        "content_digest": batch.content_digest,
        "canonical_corpus_sha256": sha256_hex(batch.canonical_corpus_bytes),
        "index_sha256": sha256_hex(index_bytes),
        **_validated_bridge_metadata(bridge_metadata),
    }
    return index, manifest


def _publication_phase(phase: str) -> None:
    """Deterministic crash/barrier seam; production behavior is intentionally empty."""
    del phase


class _GenerationMoved(RuntimeError):
    """The pointer-to-generation race overlapped safe retention pruning."""


class PublicationBudgetExceeded(GenerationStoreError):
    """A supervised bridge may not replace the pointer outside its budget."""


@dataclass(frozen=True)
class _PublicationBudget:
    deadline: float
    max_rss_bytes: int


_PUBLICATION_BUDGET: ContextVar[_PublicationBudget | None] = ContextVar(
    "om_bm25_publication_budget",
    default=None,
)


@contextmanager
def supervised_publication_budget(*, deadline: float, max_rss_bytes: int):
    """Install the fixed pre-pointer deadline/RSS boundary for one worker."""
    if not time.monotonic() < deadline or max_rss_bytes <= 0:
        raise PublicationBudgetExceeded("native-memory publication budget is already exhausted")
    token = _PUBLICATION_BUDGET.set(_PublicationBudget(deadline, max_rss_bytes))
    try:
        yield
    finally:
        _PUBLICATION_BUDGET.reset(token)


def _check_publication_budget() -> None:
    budget = _PUBLICATION_BUDGET.get()
    if budget is None:
        return
    if time.monotonic() >= budget.deadline:
        raise PublicationBudgetExceeded("native-memory publication deadline expired before commit")
    if resource is None:
        raise PublicationBudgetExceeded("native-memory publication requires POSIX resource accounting")
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    if peak_bytes > budget.max_rss_bytes:
        raise PublicationBudgetExceeded("native-memory worker RSS high-water exceeded before commit")


class BM25GenerationStore:
    """The sole schema, digest, publication, and persisted-byte BM25 authority."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))

    def has_generation_state(self) -> bool:
        try:
            root = _secure_open_root(
                self.root,
                create=False,
                writable=False,
                require_mode=False,
            )
        except FileNotFoundError:
            return False
        try:
            names = set(os.listdir(root))
            has_state = bool(names.intersection({"current-generation.json", "generations", "staging"}))
            if has_state and (os.fstat(root).st_uid != os.getuid() or (os.fstat(root).st_mode & 0o777) != 0o700):
                raise GenerationStoreError("generation store root is unsafe")
            return has_state
        finally:
            os.close(root)

    @staticmethod
    def _parse_pointer(raw: bytes) -> dict[str, Any]:
        pointer = _parse_canonical_object(raw, "pointer")
        if set(pointer) != {
            "schema",
            "generation_id",
            "index_sha256",
            "manifest_sha256",
        }:
            raise GenerationStoreError("generation pointer has unsupported claims")
        if pointer.get("schema") != CURRENT_POINTER_SCHEMA:
            raise GenerationStoreError("generation pointer schema is invalid")
        generation_id = pointer.get("generation_id")
        if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
            raise GenerationStoreError("current generation ID is not content-addressed")
        return pointer

    def _read_generation(self, root: int, generation_id: str) -> VerifiedBM25Generation:
        if _GENERATION_ID.fullmatch(generation_id) is None:
            raise GenerationStoreError("current generation ID is not content-addressed")
        generations = _open_dir(root, "generations", create=False, writable=False)
        generation = None
        pinned = False
        try:
            try:
                generation = _open_dir(generations, generation_id, create=False, writable=False)
            except GenerationStoreError as exc:
                cause = exc.__cause__
                if isinstance(cause, OSError) and cause.errno == errno.ENOENT:
                    raise _GenerationMoved(generation_id) from exc
                raise
            if not _try_generation_lock(generation, exclusive=False):
                raise _GenerationMoved(generation_id)
            pinned = True
            try:
                named = os.stat(generation_id, dir_fd=generations, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise _GenerationMoved(generation_id) from exc
            opened = os.fstat(generation)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise _GenerationMoved(generation_id)
            index_bytes = _read_file(generation, "index.json")
            manifest_bytes = _read_file(generation, "manifest.json")
        finally:
            if generation is not None:
                if pinned:
                    _unlock(generation)
                os.close(generation)
            os.close(generations)
        index, manifest = _verify_bm25_generation(index_bytes, manifest_bytes)
        if manifest.get("generation_id") != generation_id:
            raise GenerationStoreError("generation directory and manifest disagree")
        return VerifiedBM25Generation(
            generation_id,
            index_bytes,
            manifest_bytes,
            index,
            manifest,
        )

    def read_current(
        self,
        transaction_or_read_root: StoreTransaction | StoreReadRoot,
    ) -> VerifiedBM25Generation:
        root = _duplicate_root_fd(transaction_or_read_root, expected_root=self.root)
        try:
            for attempt in range(3):
                try:
                    pointer_bytes = _read_file(
                        root,
                        "current-generation.json",
                        max_bytes=1024 * 1024,
                        retry_atomic_replacement=True,
                    )
                except _PointerChanged:
                    if attempt == 2:
                        raise GenerationStoreError("generation pointer did not stabilize") from None
                    continue
                pointer = self._parse_pointer(pointer_bytes)
                try:
                    verified = self._read_generation(root, pointer["generation_id"])
                except _GenerationMoved:
                    if attempt == 2:
                        raise GenerationStoreError("generation pointer did not stabilize") from None
                    continue
                if pointer["index_sha256"] != sha256_hex(verified.index_bytes):
                    raise GenerationStoreError("generation pointer index digest is invalid")
                if pointer["manifest_sha256"] != sha256_hex(verified.manifest_bytes):
                    raise GenerationStoreError("generation pointer manifest digest is invalid")
                if isinstance(transaction_or_read_root, StoreTransaction):
                    validate_canonical_root(transaction_or_read_root)
                else:
                    _validate_canonical_read_root(transaction_or_read_root)
                return verified
            raise AssertionError("bounded pointer loop did not return or raise")
        finally:
            os.close(root)

    def _prune_generations(
        self,
        transaction: StoreTransaction,
        *,
        protected_generation_ids: frozenset[str],
    ) -> None:
        """Retain four generations without touching pointer targets or reader pins."""
        _require_store_transaction(transaction, self.root)
        root = _duplicate_root_fd(transaction, expected_root=self.root)
        generations = staging = None
        try:
            generations = _open_dir(root, "generations", create=False, writable=True)
            staging = _open_dir(root, "staging", create=True, writable=True)
            for retired_name in sorted(
                name for name in os.listdir(staging) if _RETIRED_GENERATION.fullmatch(name) is not None
            ):
                retired = _open_dir(staging, retired_name, create=False, writable=True)
                try:
                    leaves = set(os.listdir(retired))
                    if not leaves.issubset({"index.json", "manifest.json"}):
                        raise GenerationStoreError("retired generation has unsupported entries")
                    for leaf in sorted(leaves):
                        info = os.stat(leaf, dir_fd=retired, follow_symlinks=False)
                        _require_regular(info, leaf)
                        os.unlink(leaf, dir_fd=retired)
                    os.fsync(retired)
                    os.rmdir(retired_name, dir_fd=staging)
                    os.fsync(staging)
                finally:
                    os.close(retired)
            entries: list[tuple[int, str]] = []
            for name in os.listdir(generations):
                if _GENERATION_ID.fullmatch(name) is None:
                    raise GenerationStoreError(f"unsafe generation directory name: {name!r}")
                generation = _open_dir(generations, name, create=False, writable=False)
                try:
                    info = os.fstat(generation)
                    entries.append((info.st_mtime_ns, name))
                finally:
                    os.close(generation)
            remaining = len(entries)
            if remaining <= MAX_RETAINED_GENERATIONS:
                return

            for _modified_ns, name in sorted(entries):
                if remaining <= MAX_RETAINED_GENERATIONS:
                    break
                if name in protected_generation_ids:
                    continue
                generation = _open_dir(generations, name, create=False, writable=True)
                locked = False
                try:
                    if not _try_generation_lock(generation, exclusive=True):
                        continue
                    locked = True
                    named = os.stat(name, dir_fd=generations, follow_symlinks=False)
                    opened = os.fstat(generation)
                    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                        raise GenerationStoreError("generation changed during retention pruning")
                    if set(os.listdir(generation)) != {"index.json", "manifest.json"}:
                        raise GenerationStoreError("generation has unsupported retention entries")
                    for leaf in ("index.json", "manifest.json"):
                        info = os.stat(leaf, dir_fd=generation, follow_symlinks=False)
                        _require_regular(info, leaf)
                    retired_name = f".retired.{name}.{uuid.uuid4().hex}"
                    os.rename(name, retired_name, src_dir_fd=generations, dst_dir_fd=staging)
                    os.fsync(generations)
                    os.fsync(staging)
                    _publication_phase("after:retention_rename")
                    os.unlink("index.json", dir_fd=generation)
                    os.unlink("manifest.json", dir_fd=generation)
                    os.fsync(generation)
                    os.rmdir(retired_name, dir_fd=staging)
                    os.fsync(staging)
                    remaining -= 1
                finally:
                    if locked:
                        _unlock(generation)
                    os.close(generation)
            validate_canonical_root(transaction)
        finally:
            if staging is not None:
                os.close(staging)
            if generations is not None:
                os.close(generations)
            os.close(root)

    def publish(
        self,
        transaction: StoreTransaction,
        immutable_batch: DocumentBatch,
        bridge_metadata: dict[str, Any] | None = None,
    ) -> VerifiedBM25Generation:
        """Publish and freshly verify one generation with fixed BM25 authority."""
        _require_store_transaction(transaction, self.root)
        _validate_batch(immutable_batch)
        index, manifest = _build_bm25_generation(
            immutable_batch,
            bridge_metadata=bridge_metadata,
        )
        generation_id = immutable_batch.generation_id
        index_bytes = canonical_json_bytes(index) + b"\n"
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        root = _duplicate_root_fd(transaction, expected_root=self.root)
        generations = staging = stage = None
        stage_name = f".{generation_id}.{uuid.uuid4().hex}"
        pointer_temp: str | None = None
        prior_pointer_bytes: bytes | None = None
        prior_generation_id: str | None = None
        pointer_replacement_started = False
        try:
            prior_pointer_bytes = _read_file(
                root,
                "current-generation.json",
                max_bytes=1024 * 1024,
            )
        except GenerationStoreError as exc:
            cause = exc.__cause__
            if not isinstance(cause, OSError) or cause.errno != errno.ENOENT:
                raise
        if prior_pointer_bytes is not None:
            prior_generation_id = self._parse_pointer(prior_pointer_bytes)["generation_id"]
        try:
            generations = _open_dir(root, "generations", create=True, writable=True)
            staging = _open_dir(root, "staging", create=True, writable=True)
            stage = _open_dir(staging, stage_name, create=True, writable=True)
            _write_file(stage, "index.json", index_bytes)
            _publication_phase("after:index_write")
            _write_file(stage, "manifest.json", manifest_bytes)
            _publication_phase("after:manifest_write")
            _publication_phase("after:file_flush")
            os.fsync(stage)
            _publication_phase("after:staging_flush")
            reopened_index = _read_file(stage, "index.json")
            reopened_manifest = _read_file(stage, "manifest.json")
            _verify_bm25_generation(reopened_index, reopened_manifest)
            _publication_phase("after:staged_verify")

            _require_store_transaction(transaction, self.root)
            try:
                os.rename(stage_name, generation_id, src_dir_fd=staging, dst_dir_fd=generations)
                os.close(stage)
                stage = None
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                existing = self._read_generation(root, generation_id)
                if existing.index_bytes != index_bytes or existing.manifest_bytes != manifest_bytes:
                    raise GenerationStoreError("immutable generation already exists with different bytes")
                for name in ("index.json", "manifest.json"):
                    try:
                        os.unlink(name, dir_fd=stage)
                    except FileNotFoundError:
                        pass
                os.close(stage)
                stage = None
                os.rmdir(stage_name, dir_fd=staging)
            _publication_phase("after:generation_rename")
            os.fsync(generations)
            _publication_phase("after:generations_flush")
            protected_generation_ids = {generation_id}
            if prior_generation_id is not None:
                protected_generation_ids.add(prior_generation_id)
            self._prune_generations(
                transaction,
                protected_generation_ids=frozenset(protected_generation_ids),
            )
            _publication_phase("after:retention_prune")

            pointer = {
                "schema": CURRENT_POINTER_SCHEMA,
                "generation_id": generation_id,
                "index_sha256": sha256_hex(index_bytes),
                "manifest_sha256": sha256_hex(manifest_bytes),
            }
            pointer_bytes = canonical_json_bytes(pointer) + b"\n"
            pointer_temp = f".current-generation.{uuid.uuid4().hex}.tmp"
            _write_file(root, pointer_temp, pointer_bytes)
            _publication_phase("after:pointer_temp_write")
            _require_store_transaction(transaction, self.root)
            validate_canonical_root(transaction)
            _publication_phase("after:root_identity")
            _check_publication_budget()
            pointer_replacement_started = True
            os.rename(
                pointer_temp,
                "current-generation.json",
                src_dir_fd=root,
                dst_dir_fd=root,
            )
            pointer_temp = None
            _publication_phase("after:pointer_replace")
            os.fsync(root)
            _publication_phase("after:root_flush")

            verified = self.read_current(transaction)
            if verified.generation_id != generation_id:
                raise GenerationStoreError("fresh reader resolved a different generation")
            if verified.manifest.get("content_digest") != immutable_batch.content_digest:
                raise GenerationStoreError("fresh reader resolved different canonical corpus bytes")
            _check_publication_budget()
            validate_canonical_root(transaction)
            _publication_phase("after:final_verify")
            return verified
        except PublicationBudgetExceeded:
            if pointer_replacement_started:
                validate_canonical_root(transaction)
                if prior_pointer_bytes is None:
                    try:
                        os.unlink("current-generation.json", dir_fd=root)
                    except FileNotFoundError:
                        pass
                else:
                    rollback_temp = f".current-generation.rollback.{uuid.uuid4().hex}.tmp"
                    try:
                        _write_file(root, rollback_temp, prior_pointer_bytes)
                        os.rename(
                            rollback_temp,
                            "current-generation.json",
                            src_dir_fd=root,
                            dst_dir_fd=root,
                        )
                    finally:
                        try:
                            os.unlink(rollback_temp, dir_fd=root)
                        except FileNotFoundError:
                            pass
                os.fsync(root)
                _publication_phase("after:budget_rollback")
            raise
        finally:
            if pointer_temp is not None:
                try:
                    os.unlink(pointer_temp, dir_fd=root)
                except FileNotFoundError:
                    pass
            if stage is not None:
                for name in ("index.json", "manifest.json"):
                    try:
                        os.unlink(name, dir_fd=stage)
                    except FileNotFoundError:
                        pass
                os.close(stage)
                if staging is not None:
                    try:
                        os.rmdir(stage_name, dir_fd=staging)
                    except OSError:
                        pass
            if staging is not None:
                os.close(staging)
            if generations is not None:
                os.close(generations)
            os.close(root)


class BM25Backend:
    """Read-only production BM25 backend; publication uses BM25GenerationStore."""

    def __init__(self, legacy_index_path: Path, *, store_root: Path | None = None) -> None:
        self._legacy_index_path = legacy_index_path
        self._store_root = store_root or legacy_index_path.parent
        self._bm25 = None
        self._documents: list[Document] = []
        self._tokenized_corpus: list[list[str]] = []
        self._generation_id: str | None = None
        self._generation_manifest: dict[str, Any] | None = None
        self._load()

    @classmethod
    def from_documents(cls, documents: list[Document]) -> BM25Backend:
        """Build a process-local index for deterministic ranking tests only."""
        instance = cls.__new__(cls)
        instance._legacy_index_path = Path("<in-memory>")
        instance._store_root = Path("<in-memory>")
        instance._bm25 = None
        instance._documents = list(documents)
        instance._tokenized_corpus = [_tokenize(document.content) for document in documents]
        instance._generation_id = None
        instance._generation_manifest = None
        instance._build_ranker()
        return instance

    def index(self, documents: list[Document]) -> None:
        if sys.platform != "win32":
            raise RuntimeError("BM25 publication requires the search generation transaction owner")
        self._documents = list(documents)
        self._tokenized_corpus = [_tokenize(document.content) for document in documents]
        self._generation_id = None
        self._generation_manifest = None
        self._build_ranker()
        self._save_legacy()

    def _build_ranker(self) -> None:
        if self._tokenized_corpus:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi(self._tokenized_corpus)
        else:
            self._bm25 = None

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.is_ready():
            return []
        tokenized_query = _tokenize(query)
        if not tokenized_query:
            return []
        scores = self._bm25.get_scores(tokenized_query)
        scored = sorted(zip(scores, self._documents), key=lambda item: item[0], reverse=True)
        positive_scored = [(score, document) for score, document in scored if score > 0]
        if positive_scored:
            return [
                SearchResult(document=document, score=float(score), rank=rank)
                for rank, (score, document) in enumerate(positive_scored[:limit], start=1)
            ]
        overlap_scored: list[tuple[float, Document]] = []
        query_terms = set(tokenized_query)
        for document, tokens in zip(self._documents, self._tokenized_corpus):
            overlap = sum(1 for token in tokens if token in query_terms)
            if overlap > 0:
                overlap_scored.append((float(overlap), document))
        overlap_scored.sort(key=lambda item: item[0], reverse=True)
        return [
            SearchResult(document=document, score=score, rank=rank)
            for rank, (score, document) in enumerate(overlap_scored[:limit], start=1)
        ]

    def is_ready(self) -> bool:
        return self._bm25 is not None and bool(self._documents)

    @property
    def committed_generation_id(self) -> str | None:
        return self._generation_id

    @property
    def committed_generation_digest(self) -> str | None:
        if self._generation_manifest is None:
            return None
        value = self._generation_manifest.get("content_digest")
        return value if isinstance(value, str) else None

    def _load(self) -> None:
        if sys.platform != "win32":
            store = BM25GenerationStore(self._store_root)
            if store.has_generation_state():
                with StoreReadRoot.open(self._store_root) as read_root:
                    verified = store.read_current(read_root)
                self._documents = [document_from_payload(value) for value in verified.index["documents"]]
                self._tokenized_corpus = [list(tokens) for tokens in verified.index["tokenized_corpus"]]
                self._generation_id = verified.generation_id
                self._generation_manifest = verified.manifest
                self._build_ranker()
                return
        self._load_legacy()

    def _save_legacy(self) -> None:
        """Write the pre-v0.10 Windows BM25 pickle format."""
        self._legacy_index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "documents": self._documents,
            "tokenized_corpus": self._tokenized_corpus,
        }
        with self._legacy_index_path.open("wb") as stream:
            pickle.dump(data, stream)

    def _load_legacy(self) -> None:
        if not self._legacy_index_path.exists():
            return
        try:
            with self._legacy_index_path.open("rb") as stream:
                data = pickle.load(stream)
            documents = data["documents"]
            tokenized = data["tokenized_corpus"]
            if not isinstance(documents, list) or not isinstance(tokenized, list):
                return
            self._documents = documents
            self._tokenized_corpus = tokenized
            self._build_ranker()
        except Exception:
            self._bm25 = None
            self._documents = []
            self._tokenized_corpus = []
