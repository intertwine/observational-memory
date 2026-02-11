"""BM25 search backend using rank-bm25."""

from __future__ import annotations

import pickle
import re
from pathlib import Path

from . import Document, SearchResult

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
    "to", "for", "of", "and", "or", "but", "not", "with", "by", "from",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip markdown/emoji, remove stopwords."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [w for w in text.split() if w and w not in _STOPWORDS]


class BM25Backend:
    """BM25 search backend. Zero external service dependencies."""

    def __init__(self, index_path: Path) -> None:
        self._index_path = index_path
        self._bm25 = None
        self._documents: list[Document] = []
        self._tokenized_corpus: list[list[str]] = []
        self._load()

    def index(self, documents: list[Document]) -> None:
        from rank_bm25 import BM25Okapi

        self._documents = documents
        self._tokenized_corpus = [_tokenize(doc.content) for doc in documents]
        if self._tokenized_corpus:
            self._bm25 = BM25Okapi(self._tokenized_corpus)
        else:
            self._bm25 = None
        self._save()

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.is_ready():
            return []

        tokenized_query = _tokenize(query)
        if not tokenized_query:
            return []

        scores = self._bm25.get_scores(tokenized_query)

        scored = sorted(
            zip(scores, self._documents),
            key=lambda x: x[0],
            reverse=True,
        )

        results = []
        for rank, (score, doc) in enumerate(scored[:limit], start=1):
            if score <= 0:
                break
            results.append(SearchResult(document=doc, score=float(score), rank=rank))
        return results

    def is_ready(self) -> bool:
        return self._bm25 is not None and len(self._documents) > 0

    def _save(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "documents": self._documents,
            "tokenized_corpus": self._tokenized_corpus,
        }
        with open(self._index_path, "wb") as f:
            pickle.dump(data, f)

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, "rb") as f:
                data = pickle.load(f)
            self._documents = data["documents"]
            self._tokenized_corpus = data["tokenized_corpus"]
            if self._tokenized_corpus:
                from rank_bm25 import BM25Okapi

                self._bm25 = BM25Okapi(self._tokenized_corpus)
        except Exception:
            self._bm25 = None
            self._documents = []
