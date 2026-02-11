"""Search backend protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from . import Document, SearchResult


@runtime_checkable
class SearchBackend(Protocol):
    """Protocol for search backends."""

    def index(self, documents: list[Document]) -> None:
        """Index a list of documents, replacing any previous index."""
        ...

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search indexed documents. Returns results sorted by relevance."""
        ...

    def is_ready(self) -> bool:
        """Return True if the backend has an index and is ready to search."""
        ...
