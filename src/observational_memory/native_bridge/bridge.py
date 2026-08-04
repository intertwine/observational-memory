"""Transactional BM25 bridge from native Codex and Claude memories."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource
from observational_memory.search.bm25 import (
    BM25_BACKEND_CONFIG,
    BM25_BACKEND_CONFIG_DIGEST,
    BM25GenerationStore,
)
from observational_memory.search.generation import (
    DOCUMENT_SCHEMA_VERSION,
    INDEX_SCHEMA_VERSION,
    MATERIALIZATION_SCHEMA_VERSION,
    DocumentBatch,
    canonical_json_bytes,
    sha256_hex,
)
from observational_memory.search.generation_store import (
    GenerationStoreError,
    StoreBusyError,
    StoreTransaction,
    validate_canonical_root,
)

from .admission import AdmissionResult, probe_admission
from .profiles import (
    STRICT_DEFAULT_PROFILE,
    TEMPORARY_LIGHT_CANARY_PROFILE,
    BridgeResourceProfile,
)
from .secure_fs import SecureAccessError, SecureRoot, assert_disjoint_output
from .sources import SourceArtifact, StableSnapshot, capture_native_snapshot

BRIDGE_POLICY_SCHEMA_VERSION = "om.native-memory-bridge.policy.v2"
BRIDGE_STATE_SCHEMA_VERSION = "om.native-memory-bridge.state.v2"
BRIDGE_STATUS_SCHEMA_VERSION = "om.native-memory-bridge.status.v2"
BRIDGE_LEDGER_SCHEMA_VERSION = "om.native-memory-bridge.ledger.v2"
BRIDGE_GENERATION_SCHEMA_VERSION = "om.native-memory-bridge.generation.v2"
BRIDGE_BACKEND_CONFIG = BM25_BACKEND_CONFIG
BRIDGE_BACKEND_CONFIG_DIGEST = BM25_BACKEND_CONFIG_DIGEST

LIGHT_CANARY_MARKER_SCHEMA = "om.native-memory-bridge.light-canary-root.v1"

_SECURE_LEAVES = (
    "state.json",
    "status.json",
    "ledger.jsonl",
    "current-generation.json",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_attempt_id(attempt_id: str) -> str:
    if len(attempt_id) != 32 or any(character not in "0123456789abcdef" for character in attempt_id):
        raise ValueError("bridge attempt ID must be 32 lowercase hexadecimal characters")
    return attempt_id


@dataclass(frozen=True)
class BridgePolicy:
    """Complete, versioned source selection and safety policy."""

    claude_projects: tuple[str, ...] = ()
    codex_allowlist: tuple[str, ...] = ("MEMORY.md", "memory_summary.md")
    raw_memory_excluded: bool = True
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    backend_name: str = "bm25"
    document_schema_version: str = DOCUMENT_SCHEMA_VERSION
    materialization_schema_version: str = MATERIALIZATION_SCHEMA_VERSION
    index_schema_version: str = INDEX_SCHEMA_VERSION
    policy_schema_version: str = BRIDGE_POLICY_SCHEMA_VERSION

    def canonical(self) -> dict[str, Any]:
        return {
            "policy_schema_version": self.policy_schema_version,
            "claude_projects": sorted(set(self.claude_projects)),
            "codex_allowlist": sorted(set(self.codex_allowlist)),
            "raw_memory_excluded": self.raw_memory_excluded,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "backend_name": self.backend_name,
            "backend_config": BRIDGE_BACKEND_CONFIG,
            "document_schema_version": self.document_schema_version,
            "materialization_schema_version": self.materialization_schema_version,
            "index_schema_version": self.index_schema_version,
        }

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self.canonical()))


@dataclass(frozen=True)
class ImmutableGeneration:
    generation_id: str
    desired_state_digest: str
    policy_digest: str
    source_ids_and_hashes: tuple[tuple[str, str], ...]
    document_schema_version: str
    materialization_schema_version: str
    index_schema_version: str
    backend_name: str
    backend_config_digest: str
    document_bytes: bytes

    @property
    def documents(self) -> tuple[Document, ...]:
        payloads = json.loads(self.document_bytes)
        return tuple(
            Document(
                doc_id=str(payload["doc_id"]),
                source=DocumentSource(str(payload["source"])),
                heading=str(payload["heading"]),
                content=str(payload["content"]),
                date=payload.get("date"),
                metadata=dict(payload.get("metadata") or {}),
                owner=payload.get("owner"),
                scope=payload.get("scope"),
                source_type=payload.get("source_type"),
            )
            for payload in payloads
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": BRIDGE_GENERATION_SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "desired_state_digest": self.desired_state_digest,
            "policy_digest": self.policy_digest,
            "sources": [
                {"source_id": source_id, "content_hash": content_hash}
                for source_id, content_hash in self.source_ids_and_hashes
            ],
            "document_schema_version": self.document_schema_version,
            "materialization_schema_version": self.materialization_schema_version,
            "index_schema_version": self.index_schema_version,
            "backend_name": self.backend_name,
            "backend_config_digest": self.backend_config_digest,
            "document_count": len(self.documents),
            "document_bytes_sha256": sha256_hex(self.document_bytes),
        }

    def batch(self) -> DocumentBatch:
        return DocumentBatch.create(
            self.documents,
            backend_name=self.backend_name,
            backend_config_digest=self.backend_config_digest,
            generation_id=self.generation_id,
            document_schema_version=self.document_schema_version,
            materialization_schema_version=self.materialization_schema_version,
            index_schema_version=self.index_schema_version,
            identity=self.manifest(),
        )


@dataclass(frozen=True)
class BridgeResult:
    status: str
    exit_code: int
    message: str
    admission: AdmissionResult
    generation_id: str | None = None
    desired_state_digest: str | None = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    resource_profile: str = STRICT_DEFAULT_PROFILE.name
    resource_limits: dict[str, Any] | None = None
    output_root: str | None = None
    durable_receipt_written: bool = True

    def __post_init__(self) -> None:
        _require_attempt_id(self.attempt_id)


class SecureBM25Backend:
    """Bridge BM25 adapter bound to the isolated generation store."""

    def __init__(self, storage: SecureRoot, transaction: StoreTransaction) -> None:
        self.storage = storage
        self.transaction = transaction
        self.store = BM25GenerationStore(storage.path)
        self._published = None

    def validate(self) -> None:
        # Import and instantiate with a harmless corpus. This proves the local
        # dependency exists without touching providers, QMD, or remote services.
        from rank_bm25 import BM25Okapi

        BM25Okapi([["bridge", "validation"]])

    def index_batch(
        self,
        batch: DocumentBatch,
        *,
        generation: ImmutableGeneration,
        snapshot: StableSnapshot,
    ) -> None:
        del snapshot
        self._published = self.store.publish(
            self.transaction,
            batch,
            bridge_metadata={
                "schema": BRIDGE_GENERATION_SCHEMA_VERSION,
                "desired_state_digest": generation.desired_state_digest,
                "policy_digest": generation.policy_digest,
                "document_bytes_sha256": sha256_hex(generation.document_bytes),
                "sources": [
                    {"source_id": source_id, "content_hash": content_hash}
                    for source_id, content_hash in generation.source_ids_and_hashes
                ],
            },
        )

    def _current(self):
        try:
            return self.store.read_current(self.transaction)
        except GenerationStoreError:
            return None

    @property
    def committed_index_digest(self) -> str | None:
        current = self._current()
        if current is None or current.manifest.get("backend_config_digest") != BRIDGE_BACKEND_CONFIG_DIGEST:
            return None
        value = current.manifest.get("index_sha256")
        return value if isinstance(value, str) else None

    @property
    def committed_generation_id(self) -> str | None:
        current = self._current()
        return current.generation_id if current is not None else None

    @property
    def committed_desired_state_digest(self) -> str | None:
        current = self._current()
        if current is None:
            return None
        value = current.manifest.get("desired_state_digest")
        return value if isinstance(value, str) else None

    def verify_commit(self, batch: DocumentBatch) -> str:
        current = self._published
        if current is None:
            raise RuntimeError("BM25 bridge has no freshly verified publication")
        if current.generation_id != batch.generation_id:
            raise RuntimeError("BM25 fresh reader resolved a different generation")
        if current.manifest.get("content_digest") != batch.content_digest:
            raise RuntimeError("BM25 fresh reader resolved different canonical corpus bytes")
        return str(current.manifest["index_sha256"])


def _source_document(artifact: SourceArtifact) -> Document:
    try:
        content = artifact.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecureAccessError(f"native-memory source is not UTF-8: {artifact.source_id}") from exc
    title = Path(artifact.relative_path).stem.replace("_", " ").replace("-", " ").title()
    return Document(
        doc_id=f"native:{artifact.agent}:{sha256_hex(artifact.source_id.encode('utf-8'))[:20]}",
        source=DocumentSource.NATIVE_MEMORY,
        heading=f"### {artifact.agent}: {title}",
        content=content,
        metadata={
            "native_agent": artifact.agent,
            "source_id": artifact.source_id,
            "relative_path": artifact.relative_path,
            "content_hash": artifact.content_hash,
        },
    )


def _documents_payload(documents: tuple[Document, ...]) -> bytes:
    return canonical_json_bytes(
        [
            {
                "doc_id": document.doc_id,
                "source": document.source.value,
                "heading": document.heading,
                "content": document.content,
                "date": document.date,
                "metadata": document.metadata,
                "owner": document.owner,
                "scope": document.scope,
                "source_type": document.source_type,
            }
            for document in documents
        ]
    )


def build_desired_state_digest(policy: BridgePolicy, snapshot: StableSnapshot) -> str:
    payload = {
        "schema": "om.native-memory-bridge.desired-state.v2",
        "inputs": [
            {"source_id": source_id, "content_hash": content_hash} for source_id, content_hash in snapshot.source_hashes
        ],
        "policy_digest": policy.digest,
        "policy": policy.canonical(),
        "claude_opt_in": sorted(set(policy.claude_projects)),
        "raw_memory_excluded": policy.raw_memory_excluded,
        "document_schema_version": policy.document_schema_version,
        "materialization_schema_version": policy.materialization_schema_version,
        "index_schema_version": policy.index_schema_version,
        "backend_name": policy.backend_name,
        "backend_config_digest": BRIDGE_BACKEND_CONFIG_DIGEST,
    }
    return sha256_hex(canonical_json_bytes(payload))


def build_generation(policy: BridgePolicy, snapshot: StableSnapshot, desired_state_digest: str) -> ImmutableGeneration:
    documents = tuple(_source_document(artifact) for artifact in snapshot.artifacts)
    document_bytes = _documents_payload(documents)
    identity = {
        "schema": BRIDGE_GENERATION_SCHEMA_VERSION,
        "desired_state_digest": desired_state_digest,
        "policy_digest": policy.digest,
        "sources": snapshot.source_hashes,
        "document_schema_version": policy.document_schema_version,
        "materialization_schema_version": policy.materialization_schema_version,
        "index_schema_version": policy.index_schema_version,
        "backend_name": policy.backend_name,
        "backend_config_digest": BRIDGE_BACKEND_CONFIG_DIGEST,
        "document_bytes_sha256": sha256_hex(document_bytes),
    }
    generation_id = sha256_hex(canonical_json_bytes(identity))
    return ImmutableGeneration(
        generation_id=generation_id,
        desired_state_digest=desired_state_digest,
        policy_digest=policy.digest,
        source_ids_and_hashes=snapshot.source_hashes,
        document_schema_version=policy.document_schema_version,
        materialization_schema_version=policy.materialization_schema_version,
        index_schema_version=policy.index_schema_version,
        backend_name=policy.backend_name,
        backend_config_digest=BRIDGE_BACKEND_CONFIG_DIGEST,
        document_bytes=document_bytes,
    )


class NativeMemoryBridge:
    """Run the approved no-wait, BM25-only bridge transaction."""

    def __init__(
        self,
        config: Config,
        policy: BridgePolicy,
        *,
        resource_profile: BridgeResourceProfile = STRICT_DEFAULT_PROFILE,
        output_root: Path | None = None,
        invocation: str = "manual",
        admission_probe: Callable[[], AdmissionResult] | None = None,
        phase_hook: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.resource_profile = resource_profile
        self.invocation = invocation
        self.admission_probe = admission_probe or partial(probe_admission, self.resource_profile)
        self.phase_hook = phase_hook
        self.now = now or _utc_now
        self.codex_root = config.codex_home / "memories"
        self.claude_root = config.claude_projects_dir
        if resource_profile.requires_temporary_output:
            if output_root is None:
                raise ValueError("temporary light-canary profile requires an explicit output root")
            self.state_dir = Path(os.path.abspath(output_root))
        elif output_root is not None:
            raise ValueError("strict default profile does not accept a temporary output override")
        else:
            self.state_dir = config.native_bridge_data_dir
        self._validate_profile_contract()

    def _validate_profile_contract(self) -> None:
        profile = self.resource_profile
        if profile not in {STRICT_DEFAULT_PROFILE, TEMPORARY_LIGHT_CANARY_PROFILE}:
            raise SecureAccessError("bridge resource profile is not one of the fixed profiles")
        if profile.manual_only and self.invocation != "manual":
            raise SecureAccessError("temporary light-canary profile is manual-only")
        if self.policy.backend_name != "bm25" or not self.policy.raw_memory_excluded:
            raise SecureAccessError("native-memory bridge requires local BM25 and raw-memory exclusion")
        if self.policy.max_file_bytes > profile.max_file_input_bytes:
            raise SecureAccessError("bridge policy exceeds the selected per-file input limit")
        if self.policy.max_total_bytes > profile.max_total_input_bytes:
            raise SecureAccessError("bridge policy exceeds the selected total input limit")
        if profile.name == TEMPORARY_LIGHT_CANARY_PROFILE.name:
            if self.policy.codex_allowlist != profile.codex_allowlist:
                raise SecureAccessError("light canary permits Codex memory_summary.md only")
            if len(self.policy.claude_projects) != 1:
                raise SecureAccessError("light canary requires exactly one explicit Claude project")
            self._assert_temporary_output_root()

    def _assert_temporary_output_root(self) -> None:
        selected = Path(os.path.realpath(self.state_dir))
        temporary_roots = {
            Path(os.path.realpath(tempfile.gettempdir())),
            Path(os.path.realpath("/private/tmp")),
            Path(os.path.realpath("/tmp")),
        }
        if not any(root != selected and root in selected.parents for root in temporary_roots):
            raise SecureAccessError("light-canary output root must be below a local temporary directory")
        assert_disjoint_output(
            selected,
            (
                self.config.memory_dir,
                self.config.search_index_dir,
                self.codex_root,
                self.claude_root,
            ),
        )

    def resource_limits(self) -> dict[str, Any]:
        profile = self.resource_profile
        return {
            "profile": profile.name,
            "deadline_seconds": profile.timeout_seconds,
            "max_tree_rss_bytes": profile.max_rss_bytes,
            "max_total_input_bytes": profile.max_total_input_bytes,
            "max_file_input_bytes": profile.max_file_input_bytes,
            "allowed_pressure": list(profile.allowed_pressure),
            "swap_fraction_max": 0.80,
            "manual_only": profile.manual_only,
            "temporary_output_root": str(self.state_dir) if profile.requires_temporary_output else None,
        }

    def _mark_phase(self, phase: str, phases: dict[str, float], started: float) -> float:
        now = time.monotonic()
        phases[phase] = round(now - started, 6)
        if self.phase_hook is not None:
            self.phase_hook(f"after:{phase}")
        return now

    def _begin_phase(self, phase: str) -> None:
        if self.phase_hook is not None:
            self.phase_hook(f"before:{phase}")

    def _open_storage(self) -> SecureRoot:
        if self.resource_profile.requires_temporary_output:
            self._assert_temporary_output_root()
            return SecureRoot(self.state_dir, writable=True, create=True)
        assert_disjoint_output(self.config.memory_dir, (self.codex_root, self.claude_root))
        # SecureRoot creates only its final component. A first-run bridge may
        # precede ordinary OM installation, so create missing ancestors and
        # then let descriptor-anchored traversal validate the complete path.
        self.config.memory_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with SecureRoot(self.config.memory_dir, writable=True, create=True):
            pass
        return SecureRoot(self.state_dir, writable=True, create=True)

    def _establish_profile_boundary(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
    ) -> None:
        if not self.resource_profile.requires_temporary_output:
            storage.establish_boundary(_SECURE_LEAVES, transaction=transaction)
            return
        marker_name = "light-canary-profile.json"
        entries = set(storage.list_entries())
        marker = self._read_json(storage, transaction, marker_name) if marker_name in entries else None
        expected = {
            "schema": LIGHT_CANARY_MARKER_SCHEMA,
            "resource_limits": self.resource_limits(),
        }
        if marker is None:
            if entries:
                raise SecureAccessError("light-canary output root is not new, empty, or dedicated")
            self._write_json(storage, transaction, marker_name, expected)
        elif marker != expected:
            raise SecureAccessError("light-canary output root belongs to a different profile")
        storage.establish_boundary(
            (*_SECURE_LEAVES, marker_name),
            transaction=transaction,
        )

    @staticmethod
    def _read_json(
        storage: SecureRoot,
        transaction: StoreTransaction,
        relative: str,
    ) -> dict[str, Any] | None:
        raw = storage.read_output_bytes(
            relative,
            transaction=transaction,
            max_bytes=4 * 1024 * 1024,
        )
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            storage.quarantine_output_leaf(relative, transaction=transaction)
            return None
        if not isinstance(parsed, dict):
            storage.quarantine_output_leaf(relative, transaction=transaction)
            return None
        return parsed

    @staticmethod
    def _write_json(
        storage: SecureRoot,
        transaction: StoreTransaction,
        relative: str,
        payload: dict[str, Any],
    ) -> None:
        storage.atomic_write_bytes(
            relative,
            canonical_json_bytes(payload) + b"\n",
            transaction=transaction,
        )

    def _write_status(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        *,
        status: str,
        message: str,
        admission: AdmissionResult,
        phases: dict[str, float],
        generation_id: str | None = None,
        desired_state_digest: str | None = None,
        retry_at: str | None = None,
        consecutive_failure_count: int = 0,
        total_failure_count: int = 0,
        telemetry: dict[str, Any] | None = None,
        attempt_id: str,
    ) -> dict[str, Any]:
        selected_telemetry = {
            "peak_tree_rss_bytes": None,
            "rss_sample_count": 0,
            **(telemetry or {}),
        }
        self._validate_telemetry(selected_telemetry)
        payload = {
            "schema": BRIDGE_STATUS_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "status": status,
            "message": message,
            "timestamp": self.now().isoformat(),
            "generation_id": generation_id,
            "desired_state_digest": desired_state_digest,
            "retry_at": retry_at,
            "consecutive_failure_count": consecutive_failure_count,
            "total_failure_count": total_failure_count,
            "admission": {
                "admitted": admission.admitted,
                "pressure": admission.pressure,
                "swap_used_bytes": admission.swap_used_bytes,
                "swap_total_bytes": admission.swap_total_bytes,
                "reason": admission.reason,
                "resource_profile": admission.resource_profile,
            },
            "resource_limits": self.resource_limits(),
            "output_root": str(self.state_dir),
            "phase_durations_seconds": phases,
            "telemetry": selected_telemetry,
        }
        if status != "running":
            validate_canonical_root(transaction)
        self._write_json(storage, transaction, "status.json", payload)
        return payload

    @staticmethod
    def _validate_telemetry(telemetry: dict[str, Any]) -> None:
        sample_count = telemetry.get("rss_sample_count")
        peak_rss = telemetry.get("peak_tree_rss_bytes")
        if not isinstance(sample_count, int) or sample_count < 0:
            raise ValueError("RSS sample count must be a non-negative integer")
        if (sample_count == 0) != (peak_rss is None):
            raise ValueError("zero RSS samples require a null peak; sampled RSS requires a peak")

    def _write_attempt_artifact(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        directory: str,
        status_payload: dict[str, Any],
    ) -> None:
        attempt_id = status_payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise RuntimeError("terminal bridge receipt has no attempt ID")
        _require_attempt_id(attempt_id)
        storage.ensure_directory(directory, transaction=transaction)
        relative = f"{directory}/{attempt_id}.json"
        try:
            storage.write_new_bytes(
                relative,
                canonical_json_bytes({**status_payload, "terminal": True}) + b"\n",
                transaction=transaction,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"bridge attempt artifact already exists: {directory}/{attempt_id}") from exc
        except OSError as exc:
            raise RuntimeError(f"cannot create bridge attempt artifact: {directory}/{attempt_id}") from exc

    def _write_terminal_receipt(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        status_payload: dict[str, Any],
    ) -> None:
        self._write_attempt_artifact(storage, transaction, "receipts", status_payload)

    def _write_pending_receipt(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        status_payload: dict[str, Any],
    ) -> None:
        self._write_attempt_artifact(storage, transaction, "pending-receipts", status_payload)

    def _append_ledger(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        status_payload: dict[str, Any],
    ) -> None:
        record = {
            **status_payload,
            "schema": BRIDGE_LEDGER_SCHEMA_VERSION,
        }
        storage.append_bytes(
            "ledger.jsonl",
            canonical_json_bytes(record) + b"\n",
            transaction=transaction,
        )

    def _finalize_terminal(
        self,
        storage: SecureRoot,
        transaction: StoreTransaction,
        payload: dict[str, Any],
        *,
        write_receipt: bool,
    ) -> None:
        ledger_started = time.monotonic()
        self._append_ledger(storage, transaction, payload)
        phases = payload.setdefault("phase_durations_seconds", {})
        phases["ledger"] = round(time.monotonic() - ledger_started, 6)
        validate_canonical_root(transaction)
        self._write_json(storage, transaction, "status.json", payload)
        if write_receipt:
            self._write_terminal_receipt(storage, transaction, payload)
        else:
            self._write_pending_receipt(storage, transaction, payload)
        validate_canonical_root(transaction)

    def _retry_at(self) -> str:
        return (self.now() + timedelta(hours=1)).isoformat()

    def _result(
        self,
        status: str,
        exit_code: int,
        message: str,
        *,
        admission: AdmissionResult,
        generation_id: str | None = None,
        desired_state_digest: str | None = None,
        attempt_id: str,
        durable_receipt_written: bool = True,
    ) -> BridgeResult:
        return BridgeResult(
            status,
            exit_code,
            message,
            admission,
            generation_id,
            desired_state_digest,
            attempt_id,
            self.resource_profile.name,
            self.resource_limits(),
            str(self.state_dir),
            durable_receipt_written,
        )

    def _record_busy_attempt(
        self,
        admission: AdmissionResult,
        *,
        attempt_id: str,
    ) -> BridgeResult:
        message = "store-root descriptor lock is busy; no durable attempt receipt was written"
        return self._result(
            "busy",
            75,
            message,
            admission=admission,
            attempt_id=attempt_id,
            durable_receipt_written=False,
        )

    def _record_unowned_attempt(
        self,
        admission: AdmissionResult,
        *,
        attempt_id: str,
        reason: str,
    ) -> BridgeResult:
        message = f"{reason}; no durable attempt receipt was written without store ownership"
        return self._result(
            "failed",
            1,
            message,
            admission=admission,
            attempt_id=attempt_id,
            durable_receipt_written=False,
        )

    @staticmethod
    def _failure_counters(
        prior_status: dict[str, Any] | None,
        *,
        failed: bool,
    ) -> tuple[int, int]:
        prior_consecutive = 0
        prior_total = 0
        if prior_status is not None:
            if isinstance(prior_status.get("consecutive_failure_count"), int):
                prior_consecutive = max(0, prior_status["consecutive_failure_count"])
            if isinstance(prior_status.get("total_failure_count"), int):
                prior_total = max(0, prior_status["total_failure_count"])
        if failed:
            return prior_consecutive + 1, prior_total + 1
        return prior_consecutive, prior_total

    def _active_retry(self, status: dict[str, Any] | None) -> tuple[bool, str | None]:
        if status is None:
            return False, None
        retry_at = status.get("retry_at")
        if retry_at is None:
            return False, None
        if not isinstance(retry_at, str):
            return True, None
        try:
            retry_time = datetime.fromisoformat(retry_at)
        except ValueError:
            return True, None
        if retry_time.tzinfo is None:
            return True, None
        return self.now() < retry_time, retry_at

    def _record_rejection(
        self,
        admission: AdmissionResult,
        message: str,
        exit_code: int,
        *,
        attempt_id: str,
        supervised: bool,
    ) -> BridgeResult:
        try:
            storage_context = self._open_storage()
        except (GenerationStoreError, SecureAccessError, OSError) as exc:
            return self._record_unowned_attempt(
                admission,
                attempt_id=attempt_id,
                reason=str(exc),
            )
        with storage_context as storage:
            try:
                transaction = storage.acquire_store_transaction()
            except StoreBusyError:
                return self._record_busy_attempt(
                    admission,
                    attempt_id=attempt_id,
                )
            except (GenerationStoreError, SecureAccessError, OSError) as exc:
                return self._record_unowned_attempt(
                    admission,
                    attempt_id=attempt_id,
                    reason=str(exc),
                )
            with transaction:
                self._establish_profile_boundary(storage, transaction)
                prior_status = self._read_json(storage, transaction, "status.json")
                consecutive, total = self._failure_counters(prior_status, failed=True)
                payload = self._write_status(
                    storage,
                    transaction,
                    status="rejected",
                    message=message,
                    admission=admission,
                    phases={},
                    retry_at=self._retry_at(),
                    consecutive_failure_count=consecutive,
                    total_failure_count=total,
                    attempt_id=attempt_id,
                )
                self._finalize_terminal(
                    storage,
                    transaction,
                    payload,
                    write_receipt=not supervised,
                )
                return self._result(
                    "rejected",
                    exit_code,
                    message,
                    admission=admission,
                    attempt_id=attempt_id,
                )

    def run(self) -> BridgeResult:
        attempt_id = uuid.uuid4().hex
        return self.run_with_admission(
            self.admission_probe(),
            attempt_id=attempt_id,
        )

    def run_with_admission(
        self,
        admission: AdmissionResult,
        *,
        supervised: bool = False,
        attempt_id: str | None = None,
    ) -> BridgeResult:
        """Run with one immutable admission result supplied by the supervisor."""
        attempt_id = _require_attempt_id(attempt_id or uuid.uuid4().hex)
        if admission.resource_profile != self.resource_profile.name:
            admission = AdmissionResult(
                False,
                "probe-error",
                admission.swap_used_bytes,
                admission.swap_total_bytes,
                "admission evidence names a different resource profile",
                self.resource_profile.name,
            )
        if not admission.admitted:
            return self._record_rejection(
                admission,
                admission.reason,
                76,
                attempt_id=attempt_id,
                supervised=supervised,
            )

        phases: dict[str, float] = {}
        phase_started = time.monotonic()
        prior_status: dict[str, Any] | None = None
        lock_started = time.monotonic()
        try:
            storage_context = self._open_storage()
        except (GenerationStoreError, SecureAccessError, OSError) as exc:
            return self._record_unowned_attempt(
                admission,
                attempt_id=attempt_id,
                reason=str(exc),
            )
        with storage_context as storage:
            try:
                transaction = storage.acquire_store_transaction()
            except StoreBusyError:
                return self._record_busy_attempt(
                    admission,
                    attempt_id=attempt_id,
                )
            except (GenerationStoreError, SecureAccessError, OSError) as exc:
                return self._record_unowned_attempt(
                    admission,
                    attempt_id=attempt_id,
                    reason=str(exc),
                )
            with transaction:
                try:
                    phases["lock"] = round(time.monotonic() - lock_started, 6)
                    self._begin_phase("secure_state")
                    self._establish_profile_boundary(storage, transaction)
                    phase_started = self._mark_phase("secure_state", phases, phase_started)
                    prior_status = self._read_json(storage, transaction, "status.json")
                    # State is an operational mirror, not the successful frontier.
                    self._read_json(storage, transaction, "state.json")
                    consecutive, total = self._failure_counters(prior_status, failed=False)
                    retry_active, retry_at = self._active_retry(prior_status)
                    manual_retry = self.invocation == "manual" and retry_at is not None
                    if retry_active and not manual_retry:
                        if retry_at is None:
                            message = "bridge retry state is invalid; retry is fail-closed"
                        else:
                            message = f"bridge retry is deferred until {retry_at}"
                        payload = self._write_status(
                            storage,
                            transaction,
                            status="deferred",
                            message=message,
                            admission=admission,
                            phases=phases,
                            retry_at=retry_at,
                            consecutive_failure_count=consecutive,
                            total_failure_count=total,
                            attempt_id=attempt_id,
                        )
                        self._finalize_terminal(
                            storage,
                            transaction,
                            payload,
                            write_receipt=not supervised,
                        )
                        return self._result(
                            "deferred",
                            75,
                            message,
                            admission=admission,
                            attempt_id=attempt_id,
                        )
                    self._begin_phase("backend_validation")
                    backend = SecureBM25Backend(storage, transaction)
                    backend.validate()
                    phase_started = self._mark_phase("backend_validation", phases, phase_started)

                    self._write_status(
                        storage,
                        transaction,
                        status="running",
                        message="native-memory bridge transaction is running",
                        admission=admission,
                        phases=phases,
                        consecutive_failure_count=consecutive,
                        total_failure_count=total,
                        attempt_id=attempt_id,
                    )

                    self._begin_phase("stable_snapshot")
                    snapshot = capture_native_snapshot(
                        codex_root=self.codex_root,
                        codex_allowlist=self.policy.codex_allowlist,
                        claude_projects_root=self.claude_root,
                        claude_projects=self.policy.claude_projects,
                        max_file_bytes=self.policy.max_file_bytes,
                        max_total_bytes=self.policy.max_total_bytes,
                    )
                    phase_started = self._mark_phase("stable_snapshot", phases, phase_started)

                    self._begin_phase("desired_state")
                    desired_digest = build_desired_state_digest(self.policy, snapshot)
                    phase_started = self._mark_phase("desired_state", phases, phase_started)
                    if (
                        backend.committed_desired_state_digest == desired_digest
                        and backend.committed_generation_id is not None
                        and backend.committed_index_digest is not None
                    ):
                        generation_id = backend.committed_generation_id
                        payload = self._write_status(
                            storage,
                            transaction,
                            status="unchanged",
                            message="successfully committed desired state is unchanged",
                            admission=admission,
                            phases=phases,
                            generation_id=generation_id,
                            desired_state_digest=desired_digest,
                            consecutive_failure_count=0,
                            total_failure_count=total,
                            attempt_id=attempt_id,
                        )
                        self._finalize_terminal(
                            storage,
                            transaction,
                            payload,
                            write_receipt=not supervised,
                        )
                        return self._result(
                            "unchanged",
                            0,
                            payload["message"],
                            admission=admission,
                            generation_id=generation_id,
                            desired_state_digest=desired_digest,
                            attempt_id=attempt_id,
                        )

                    self._begin_phase("immutable_generation")
                    generation = build_generation(self.policy, snapshot, desired_digest)
                    phase_started = self._mark_phase("immutable_generation", phases, phase_started)
                    self._begin_phase("materialization")
                    batch = generation.batch()
                    phase_started = self._mark_phase("materialization", phases, phase_started)

                    self._begin_phase("index")
                    backend.index_batch(
                        batch,
                        generation=generation,
                        snapshot=snapshot,
                    )
                    phase_started = self._mark_phase("index", phases, phase_started)
                    self._begin_phase("verify_commit")
                    index_content_digest = backend.verify_commit(batch)
                    phase_started = self._mark_phase("verify_commit", phases, phase_started)

                    self._begin_phase("publish_state")
                    status_payload = self._write_status(
                        storage,
                        transaction,
                        status="success",
                        message="native-memory generation committed",
                        admission=admission,
                        phases=phases,
                        generation_id=generation.generation_id,
                        desired_state_digest=desired_digest,
                        consecutive_failure_count=0,
                        total_failure_count=total,
                        attempt_id=attempt_id,
                    )
                    state_payload = {
                        **status_payload,
                        "schema": BRIDGE_STATE_SCHEMA_VERSION,
                        "policy_digest": generation.policy_digest,
                        "source_ids_and_hashes": generation.source_ids_and_hashes,
                        "document_schema_version": generation.document_schema_version,
                        "materialization_schema_version": generation.materialization_schema_version,
                        "index_schema_version": generation.index_schema_version,
                        "backend_name": generation.backend_name,
                        "backend_config_digest": generation.backend_config_digest,
                        "index_content_digest": index_content_digest,
                    }
                    self._write_json(storage, transaction, "state.json", state_payload)
                    phase_started = self._mark_phase("publish_state", phases, phase_started)
                    self._begin_phase("ledger")
                    self._append_ledger(storage, transaction, status_payload)
                    self._mark_phase("ledger", phases, phase_started)
                    validate_canonical_root(transaction)
                    self._write_json(storage, transaction, "status.json", status_payload)
                    if supervised:
                        self._write_pending_receipt(storage, transaction, status_payload)
                    else:
                        self._write_terminal_receipt(storage, transaction, status_payload)
                    validate_canonical_root(transaction)
                    return self._result(
                        "success",
                        0,
                        status_payload["message"],
                        admission=admission,
                        generation_id=generation.generation_id,
                        desired_state_digest=desired_digest,
                        attempt_id=attempt_id,
                    )
                except Exception as exc:
                    try:
                        validate_canonical_root(transaction)
                    except Exception:
                        return self._result(
                            "failed",
                            1,
                            f"{exc}; canonical root changed, so no durable attempt receipt was written",
                            admission=admission,
                            attempt_id=attempt_id,
                            durable_receipt_written=False,
                        )
                    self._establish_profile_boundary(storage, transaction)
                    consecutive, total = self._failure_counters(prior_status, failed=True)
                    payload = self._write_status(
                        storage,
                        transaction,
                        status="failed",
                        message=str(exc),
                        admission=admission,
                        phases=phases,
                        retry_at=self._retry_at(),
                        consecutive_failure_count=consecutive,
                        total_failure_count=total,
                        attempt_id=attempt_id,
                    )
                    self._finalize_terminal(
                        storage,
                        transaction,
                        payload,
                        write_receipt=not supervised,
                    )
                    return self._result(
                        "failed",
                        1,
                        str(exc),
                        admission=admission,
                        attempt_id=attempt_id,
                    )

    def record_supervisor_telemetry(self, attempt_id: str, telemetry: dict[str, Any]) -> None:
        """Finalize telemetry for exactly one child attempt, never the latest attempt."""
        _require_attempt_id(attempt_id)
        with self._open_storage() as storage:
            try:
                transaction = storage.acquire_store_transaction()
            except StoreBusyError as exc:
                raise RuntimeError(
                    "cannot finalize supervisor telemetry while the store root is owned; no receipt was written"
                ) from exc
            with transaction:
                self._establish_profile_boundary(storage, transaction)
                pending = self._read_json(
                    storage,
                    transaction,
                    f"pending-receipts/{attempt_id}.json",
                )
                if pending is None or pending.get("attempt_id") != attempt_id or pending.get("terminal") is not True:
                    raise RuntimeError(f"supervised bridge attempt has no exact pending receipt: {attempt_id}")
                finalized = dict(pending)
                finalized.pop("terminal", None)
                finalized["telemetry"] = {**dict(finalized.get("telemetry") or {}), **telemetry}
                self._validate_telemetry(finalized["telemetry"])
                validate_canonical_root(transaction)
                self._write_terminal_receipt(storage, transaction, finalized)
                latest = self._read_json(storage, transaction, "status.json")
                if latest is not None and latest.get("attempt_id") == attempt_id:
                    self._write_json(storage, transaction, "status.json", finalized)
                validate_canonical_root(transaction)

    def record_external_failure(
        self,
        message: str,
        *,
        admission: AdmissionResult,
        telemetry: dict[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> BridgeResult:
        """Record an outer timeout/RSS failure after the child process is gone."""
        attempt_id = _require_attempt_id(attempt_id or uuid.uuid4().hex)
        try:
            storage_context = self._open_storage()
        except (GenerationStoreError, SecureAccessError, OSError) as exc:
            return self._record_unowned_attempt(
                admission,
                attempt_id=attempt_id,
                reason=f"{message}; {exc}",
            )
        with storage_context as storage:
            try:
                transaction = storage.acquire_store_transaction()
            except StoreBusyError:
                return self._result(
                    "failed",
                    1,
                    f"{message}; store root is owned, so no durable attempt receipt was written",
                    admission=admission,
                    attempt_id=attempt_id,
                    durable_receipt_written=False,
                )
            except (GenerationStoreError, SecureAccessError, OSError) as exc:
                return self._record_unowned_attempt(
                    admission,
                    attempt_id=attempt_id,
                    reason=f"{message}; {exc}",
                )
            with transaction:
                self._establish_profile_boundary(storage, transaction)
                prior_status = self._read_json(storage, transaction, "status.json")
                consecutive, total = self._failure_counters(prior_status, failed=True)
                selected_telemetry = {
                    "peak_tree_rss_bytes": None,
                    "rss_sample_count": 0,
                    **(telemetry or {}),
                }
                self._validate_telemetry(selected_telemetry)
                payload = {
                    "schema": BRIDGE_STATUS_SCHEMA_VERSION,
                    "attempt_id": attempt_id,
                    "status": "failed",
                    "message": message,
                    "timestamp": self.now().isoformat(),
                    "generation_id": None,
                    "desired_state_digest": None,
                    "retry_at": self._retry_at(),
                    "consecutive_failure_count": consecutive,
                    "total_failure_count": total,
                    "admission": {
                        "admitted": admission.admitted,
                        "pressure": admission.pressure,
                        "swap_used_bytes": admission.swap_used_bytes,
                        "swap_total_bytes": admission.swap_total_bytes,
                        "reason": admission.reason,
                        "resource_profile": admission.resource_profile,
                    },
                    "resource_limits": self.resource_limits(),
                    "output_root": str(self.state_dir),
                    "phase_durations_seconds": {},
                    "telemetry": selected_telemetry,
                }
                validate_canonical_root(transaction)
                self._write_terminal_receipt(storage, transaction, payload)
                self._append_ledger(storage, transaction, payload)
                self._write_json(storage, transaction, "status.json", payload)
                validate_canonical_root(transaction)
                return self._result(
                    "failed",
                    1,
                    message,
                    admission=admission,
                    attempt_id=attempt_id,
                )
