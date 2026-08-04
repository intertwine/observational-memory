"""Immutable document generations shared by materialization and search."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

from . import Document

DOCUMENT_SCHEMA_VERSION = "om.search.document.v2"
MATERIALIZATION_SCHEMA_VERSION = "om.materialization.v2"
INDEX_SCHEMA_VERSION = "om.search.index.v2"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a value in the one canonical JSON form used for generation IDs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backend_config(config, backend_name: str | None = None) -> dict[str, Any]:
    """Return non-secret backend configuration that affects index output."""
    name = backend_name or config.search_backend
    if name == "bm25":
        # The fixed BM25 authority owns this accepted configuration. Import at
        # call time to avoid a module cycle while generation types initialize.
        from .bm25 import BM25_BACKEND_CONFIG

        return dict(BM25_BACKEND_CONFIG)
    if name in {"qmd", "qmd-hybrid"}:
        return {
            "name": name,
            "index_name": config.qmd_index_name,
            "no_rerank": bool(config.qmd_no_rerank),
            "embed_model": config.qmd_embed_model,
            "rerank_model": config.qmd_rerank_model,
            "generate_model": config.qmd_generate_model,
        }
    if name == "moss":
        key_digest = sha256_hex((config.moss_project_key or "").encode("utf-8"))
        return {
            "name": "moss",
            "project_id": config.moss_project_id,
            "project_key_digest": key_digest,
            "index_name": config.moss_index_name,
            "model_id": config.moss_model_id,
            "alpha": config.moss_alpha,
        }
    return {"name": name}


def backend_config_digest(config, backend_name: str | None = None) -> str:
    return sha256_hex(canonical_json_bytes(backend_config(config, backend_name)))


def document_payload(document: Document) -> dict[str, Any]:
    return {
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


def document_from_payload(payload: dict[str, Any]) -> Document:
    from . import DocumentSource

    return Document(
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


@dataclass(frozen=True)
class DocumentBatch:
    """One immutable, content-addressed set of documents for one backend commit."""

    generation_id: str
    document_bytes: tuple[bytes, ...]
    backend_name: str
    backend_config_digest: str
    document_schema_version: str = DOCUMENT_SCHEMA_VERSION
    materialization_schema_version: str = MATERIALIZATION_SCHEMA_VERSION
    index_schema_version: str = INDEX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        payloads = tuple(json.loads(document_bytes) for document_bytes in self.document_bytes)
        if any(
            canonical_json_bytes(payload) != document_bytes
            for payload, document_bytes in zip(payloads, self.document_bytes)
        ):
            raise ValueError("document batch contains non-canonical document bytes")
        generation_ids = {str(payload["metadata"].get("generation_id")) for payload in payloads}
        if generation_ids and generation_ids != {self.generation_id}:
            raise ValueError("document batch contains more than one generation")

    @property
    def documents(self) -> tuple[Document, ...]:
        """Return fresh mutable views; the batch retains only immutable bytes."""
        return tuple(document_from_payload(json.loads(document_bytes)) for document_bytes in self.document_bytes)

    @property
    def canonical_corpus_bytes(self) -> bytes:
        """Return the exact canonical corpus bytes covered by the commit digest."""
        return b"[" + b",".join(self.document_bytes) + b"]"

    @property
    def content_digest(self) -> str:
        """Bind one generation token to the exact canonical corpus bytes."""
        payload = {
            "generation_id": self.generation_id,
            "document_schema_version": self.document_schema_version,
            "materialization_schema_version": self.materialization_schema_version,
            "index_schema_version": self.index_schema_version,
            "backend_name": self.backend_name,
            "backend_config_digest": self.backend_config_digest,
            "canonical_corpus_sha256": sha256_hex(self.canonical_corpus_bytes),
        }
        return sha256_hex(canonical_json_bytes(payload))

    @classmethod
    def create(
        cls,
        documents: Iterable[Document],
        *,
        backend_name: str,
        backend_config_digest: str,
        generation_id: str | None = None,
        document_schema_version: str = DOCUMENT_SCHEMA_VERSION,
        materialization_schema_version: str = MATERIALIZATION_SCHEMA_VERSION,
        index_schema_version: str = INDEX_SCHEMA_VERSION,
        identity: dict[str, Any] | None = None,
    ) -> DocumentBatch:
        original = tuple(documents)
        payload = {
            "document_schema_version": document_schema_version,
            "materialization_schema_version": materialization_schema_version,
            "index_schema_version": index_schema_version,
            "backend_name": backend_name,
            "backend_config_digest": backend_config_digest,
            "identity": identity or {},
            "documents": [document_payload(document) for document in original],
        }
        computed = sha256_hex(canonical_json_bytes(payload))
        selected_id = generation_id or computed
        frozen_documents = tuple(
            replace(document, metadata={**document.metadata, "generation_id": selected_id}) for document in original
        )
        return cls(
            generation_id=selected_id,
            document_bytes=tuple(canonical_json_bytes(document_payload(document)) for document in frozen_documents),
            backend_name=backend_name,
            backend_config_digest=backend_config_digest,
            document_schema_version=document_schema_version,
            materialization_schema_version=materialization_schema_version,
            index_schema_version=index_schema_version,
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "document_schema_version": self.document_schema_version,
            "materialization_schema_version": self.materialization_schema_version,
            "index_schema_version": self.index_schema_version,
            "backend_name": self.backend_name,
            "backend_config_digest": self.backend_config_digest,
            "document_count": len(self.document_bytes),
            "content_digest": self.content_digest,
        }
