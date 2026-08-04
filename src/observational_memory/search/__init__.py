"""Pluggable search over observational memory files."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator


class DocumentSource(Enum):
    OBSERVATIONS = "observations"
    REFLECTIONS = "reflections"
    AUTO_MEMORY = "auto_memory"
    NATIVE_MEMORY = "native_memory"


@dataclass
class Document:
    """A searchable unit of memory content."""

    doc_id: str  # e.g. "obs:2026-02-10" or "ref:active-projects"
    source: DocumentSource
    heading: str  # e.g. "## 2026-02-10" or "## Active Projects"
    content: str  # full text of the section (heading included)
    date: str | None = None  # YYYY-MM-DD if from observations
    metadata: dict = field(default_factory=dict)
    # Gate 3 typed provenance copies that RIDE on retrieval objects (inline
    # Markdown metadata stays authoritative — these are derived labels only).
    # `source_type` (not `source`) avoids colliding with `source: DocumentSource`;
    # it carries the inline provenance origin (e.g. "inferred"/"stated"). `owner`
    # maps 1:1 from the inline `node` value. All default None so every existing
    # parser/backend/test constructs unchanged.
    owner: str | None = None
    # `scope` is populated only on the LOCAL parse path (parse_reflections via
    # derive_section_provenance). It is intentionally NEVER encoded to the Moss
    # cloud and so always comes back None for Moss-retrieved results — do not
    # assume cross-backend parity for this field (it is leak-critical that scope
    # is not round-tripped through the cloud index).
    scope: str | None = None
    source_type: str | None = None


@dataclass
class SearchResult:
    """A single search hit."""

    document: Document
    score: float
    rank: int


def get_backend(backend_name: str, config):
    """Resolve a backend name to an instance."""
    if backend_name == "bm25":
        from .bm25 import BM25Backend

        return BM25Backend(config.search_index_dir / "bm25.pkl", store_root=config.search_index_dir)
    elif backend_name == "qmd":
        from .qmd import QMDBackend

        return QMDBackend(
            config.memory_dir,
            mode="search",
            index_name=config.qmd_index_name,
            no_rerank=config.qmd_no_rerank,
            model_env=config.qmd_model_env(),
        )
    elif backend_name == "qmd-hybrid":
        from .qmd import QMDBackend

        return QMDBackend(
            config.memory_dir,
            mode="query",
            index_name=config.qmd_index_name,
            no_rerank=config.qmd_no_rerank,
            model_env=config.qmd_model_env(),
        )
    elif backend_name == "moss":
        from .moss import MossBackend
        from .none import NoneBackend

        creds = config.moss_credentials()
        if creds is None:
            # Opt-in backend with no usable creds: fail closed to a no-op so the
            # CLI degrades to an ungrounded experience instead of crashing.
            return NoneBackend()
        project_id, project_key = creds
        return MossBackend(
            project_id=project_id,
            project_key=project_key,
            index_name=config.moss_index_name,
            model_id=config.moss_model_id,
            alpha=config.moss_alpha,
        )
    elif backend_name == "none":
        from .none import NoneBackend

        return NoneBackend()
    else:
        raise ValueError(
            f"Unknown search backend: {backend_name!r}. Use 'bm25', 'qmd', 'qmd-hybrid', 'moss', or 'none'."
        )


def _stable_read_text(path: Path) -> str | None:
    """Read one regular file exactly, rejecting replacement or truncation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"search input is not a regular file: {path}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError(f"search input was truncated while reading: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise RuntimeError(f"search input grew while reading: {path}")
        after = os.fstat(fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise RuntimeError(f"search input changed while reading: {path}")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


@contextmanager
def _generation_transaction(config) -> Iterator[object]:
    """Acquire the ordinary BM25 root owner before capture or publication."""
    from .generation_store import StoreTransaction

    with StoreTransaction.acquire(
        config.search_index_dir,
        timeout_seconds=10.0,
        create=True,
    ) as transaction:
        yield transaction


def _capture_document_batch_owned(config, transaction: object):
    """Capture all ordinary search inputs once into an immutable batch."""
    from .generation_store import _require_store_transaction

    _require_store_transaction(transaction, config.search_index_dir)
    from .generation import DocumentBatch, backend_config_digest
    from .parser import parse_auto_memory, parse_observations_content, parse_reflections_content

    documents = []
    observations = _stable_read_text(config.observations_path)
    if observations is not None:
        documents.extend(parse_observations_content(observations, source_path=config.observations_path))
    reflections = _stable_read_text(config.reflections_path)
    if reflections is not None:
        documents.extend(parse_reflections_content(reflections, source_path=config.reflections_path))
    # Preserve the legacy Claude auto-memory corpus for every ordinary reindex.
    # The list is captured into the batch before a backend sees any document.
    documents.extend(parse_auto_memory(config.claude_projects_dir))
    return DocumentBatch.create(
        documents,
        backend_name=config.search_backend,
        backend_config_digest=backend_config_digest(config),
    )


def _commit_document_batch_owned(config, batch, transaction: object) -> int:
    """Commit and verify exactly one immutable generation."""
    from .generation import backend_config_digest
    from .generation_store import _require_store_transaction

    _require_store_transaction(transaction, config.search_index_dir)

    if batch.backend_name != config.search_backend:
        raise ValueError("document batch backend does not match configured backend")
    if batch.backend_config_digest != backend_config_digest(config):
        raise ValueError("document batch backend configuration changed before commit")

    if config.search_backend != "bm25":
        raise RuntimeError("generation transactions are reserved for the local BM25 backend")
    from .bm25 import BM25GenerationStore

    verified = BM25GenerationStore(config.search_index_dir).publish(transaction, batch)
    if verified.generation_id != batch.generation_id:
        raise RuntimeError("fresh BM25 reader resolved a different generation")
    if verified.manifest.get("content_digest") != batch.content_digest:
        raise RuntimeError("fresh BM25 reader resolved different canonical corpus bytes")
    return len(batch.documents)


def reindex(config) -> int:
    """Rebuild the configured search index.

    Returns:
        Number of documents indexed.
    """
    if config.search_backend == "bm25" and sys.platform != "win32":
        with _generation_transaction(config) as transaction:
            batch = _capture_document_batch_owned(config, transaction)
            return _commit_document_batch_owned(config, batch, transaction)

    from .parser import parse_auto_memory, parse_observations, parse_reflections

    documents = []
    documents.extend(parse_observations(config.observations_path))
    documents.extend(parse_reflections(config.reflections_path))
    documents.extend(parse_auto_memory(config.claude_projects_dir))
    backend = get_backend(config.search_backend, config)
    backend.index(documents)
    return len(documents)
