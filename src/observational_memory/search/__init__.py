"""Pluggable search over observational memory files."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class DocumentSource(Enum):
    OBSERVATIONS = "observations"
    REFLECTIONS = "reflections"


@dataclass
class Document:
    """A searchable unit of memory content."""

    doc_id: str  # e.g. "obs:2026-02-10" or "ref:active-projects"
    source: DocumentSource
    heading: str  # e.g. "## 2026-02-10" or "## Active Projects"
    content: str  # full text of the section (heading included)
    date: str | None = None  # YYYY-MM-DD if from observations
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    """A single search hit."""

    document: Document
    score: float
    rank: int


def get_backend(backend_name: str, config) -> "backend.SearchBackend":
    """Resolve a backend name to an instance."""
    if backend_name == "bm25":
        from .bm25 import BM25Backend

        index_path = config.memory_dir / ".search-index" / "bm25.pkl"
        return BM25Backend(index_path)
    elif backend_name == "qmd":
        from .qmd import QMDBackend

        return QMDBackend(config.memory_dir, mode="search")
    elif backend_name == "qmd-hybrid":
        from .qmd import QMDBackend

        return QMDBackend(config.memory_dir, mode="query")
    elif backend_name == "none":
        from .none import NoneBackend

        return NoneBackend()
    else:
        raise ValueError(
            f"Unknown search backend: {backend_name!r}. "
            "Use 'bm25', 'qmd', 'qmd-hybrid', or 'none'."
        )


def reindex(config) -> int:
    """Parse memory files and rebuild the search index.

    Returns:
        Number of documents indexed.
    """
    from .parser import parse_observations, parse_reflections

    documents = []
    documents.extend(parse_observations(config.observations_path))
    documents.extend(parse_reflections(config.reflections_path))

    backend = get_backend(config.search_backend, config)
    backend.index(documents)
    return len(documents)
