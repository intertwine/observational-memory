"""Pluggable search over observational memory files."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DocumentSource(Enum):
    OBSERVATIONS = "observations"
    REFLECTIONS = "reflections"
    AUTO_MEMORY = "auto_memory"


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

        index_path = config.memory_dir / ".search-index" / "bm25.pkl"
        return BM25Backend(index_path)
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


def reindex(config) -> int:
    """Parse memory files and rebuild the search index.

    Returns:
        Number of documents indexed.
    """
    from .parser import parse_auto_memory, parse_observations, parse_reflections

    documents = []
    documents.extend(parse_observations(config.observations_path))
    documents.extend(parse_reflections(config.reflections_path))
    documents.extend(parse_auto_memory(config.claude_projects_dir))

    backend = get_backend(config.search_backend, config)
    backend.index(documents)
    return len(documents)
