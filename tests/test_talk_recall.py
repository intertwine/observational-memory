"""Tests for the talk RecallEngine."""

from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource, SearchResult
from observational_memory.talk.recall import RecallEngine


class _FakeBackend:
    def __init__(self, ready=True, results=None, raise_on_search=False):
        self._ready = ready
        self._results = results or []
        self._raise = raise_on_search

    def is_ready(self):
        return self._ready

    def search(self, query, limit=10):
        if self._raise:
            raise RuntimeError("backend exploded")
        return self._results[:limit]

    def index(self, documents):
        pass


def _result(doc_id, content, rank, score=1.0, source=DocumentSource.REFLECTIONS):
    return SearchResult(
        document=Document(doc_id=doc_id, source=source, heading=f"## {doc_id}", content=content),
        score=score,
        rank=rank,
    )


def test_recall_shapes_and_trims_snippets():
    long_content = "x" * 2000
    backend = _FakeBackend(results=[_result("ref:a", long_content, 1)])
    engine = RecallEngine(Config(), backend, snippet_max_chars=100)
    try:
        result = engine.recall("anything")
        assert result.backend_ready is True
        assert result.grounded is True
        assert len(result.snippets) == 1
        assert len(result.snippets[0].content) <= 102  # 100 + " …"
        assert result.snippets[0].content.endswith("…")
    finally:
        engine.close()


def test_recall_dedupes_by_doc_id():
    backend = _FakeBackend(results=[_result("ref:a", "one", 1), _result("ref:a", "dup", 2)])
    engine = RecallEngine(Config(), backend)
    try:
        result = engine.recall("q")
        assert len(result.snippets) == 1
    finally:
        engine.close()


def test_recall_empty_when_backend_not_ready():
    engine = RecallEngine(Config(), _FakeBackend(ready=False, results=[_result("ref:a", "x", 1)]))
    try:
        result = engine.recall("q")
        assert result.backend_ready is False
        assert result.snippets == []
        assert result.grounded is False
    finally:
        engine.close()


def test_recall_swallows_search_errors():
    engine = RecallEngine(Config(), _FakeBackend(raise_on_search=True))
    try:
        result = engine.recall("q")
        assert result.backend_ready is True
        assert result.snippets == []
    finally:
        engine.close()


def test_recall_async_returns_same_result():
    backend = _FakeBackend(results=[_result("ref:a", "hello world", 1)])
    engine = RecallEngine(Config(), backend)
    try:
        result = engine.recall_async("hello").result(timeout=5)
        assert result.grounded is True
        assert result.snippets[0].doc_id == "ref:a"
    finally:
        engine.close()


def test_recall_blank_query_is_safe():
    engine = RecallEngine(Config(), _FakeBackend())
    try:
        result = engine.recall("   ")
        assert result.snippets == []
    finally:
        engine.close()
