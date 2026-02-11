"""No-op search backend."""

from __future__ import annotations

from . import Document, SearchResult


class NoneBackend:
    """No-op backend. Always falls back to full file dump."""

    def index(self, documents: list[Document]) -> None:
        pass

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return []

    def is_ready(self) -> bool:
        return False
