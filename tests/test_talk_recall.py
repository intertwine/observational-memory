"""Tests for the talk RecallEngine."""

import threading
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource, SearchResult
from observational_memory.talk.recall import RecallEngine, RecallStatus


class _BlockingBackend:
    """Backend whose search() blocks until released — models a hung query."""

    def __init__(self):
        self._release = threading.Event()
        self.started = threading.Event()

    def is_ready(self):
        return True

    def search(self, query, limit=10):
        self.started.set()
        self._release.wait(timeout=10)
        return []

    def release(self):
        self._release.set()

    def index(self, documents):
        pass


def test_recall_async_runs_on_daemon_thread_so_a_hung_search_cannot_block_exit():
    # Codex P1: a wedged backend.search() must never keep the process alive. The
    # background recall thread must be a daemon (killed at interpreter exit, not
    # joined), and the caller's timeout must fire while it is still running.
    backend = _BlockingBackend()
    engine = RecallEngine(Config(), backend)
    try:
        future = engine.recall_async("q")
        assert backend.started.wait(timeout=2), "recall thread should have started"

        # The caller's budget fires while the search is still wedged.
        with pytest.raises(FutureTimeoutError):
            future.result(timeout=0.05)
        assert engine.has_pending_recall() is True

        # The wedged work is on a daemon thread, so it cannot block process exit.
        recall_threads = [t for t in threading.enumerate() if t.name == "om-recall"]
        assert recall_threads, "expected a background om-recall thread"
        assert all(t.daemon for t in recall_threads), "recall thread must be a daemon"
    finally:
        backend.release()
        engine.close()


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


def test_recall_status_ok_when_hits():
    backend = _FakeBackend(results=[_result("ref:a", "hello", 1)])
    engine = RecallEngine(Config(), backend)
    try:
        result = engine.recall("q")
        assert result.status is RecallStatus.OK
        assert result.grounded is True
    finally:
        engine.close()


def test_recall_status_empty_when_ready_no_hits():
    engine = RecallEngine(Config(), _FakeBackend(results=[]))
    try:
        result = engine.recall("q")
        assert result.status is RecallStatus.EMPTY
        assert result.backend_ready is True
        assert result.grounded is False
    finally:
        engine.close()


def test_recall_status_unavailable_when_backend_not_ready():
    engine = RecallEngine(Config(), _FakeBackend(ready=False))
    try:
        result = engine.recall("q")
        assert result.status is RecallStatus.UNAVAILABLE
        assert result.backend_ready is False
    finally:
        engine.close()


def test_recall_status_unavailable_on_search_exception():
    # A backend that *threw* is not evidence of an empty corpus — guard the §1
    # refinement that classifies in-search errors as UNAVAILABLE, not EMPTY.
    engine = RecallEngine(Config(), _FakeBackend(raise_on_search=True))
    try:
        result = engine.recall("q")
        assert result.status is RecallStatus.UNAVAILABLE
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


def test_has_pending_recall_tracks_inflight():
    import threading

    release = threading.Event()

    class _BlockingBackend(_FakeBackend):
        def search(self, query, limit=10):
            release.wait(timeout=5)
            return []

    engine = RecallEngine(Config(), _BlockingBackend())
    try:
        assert engine.has_pending_recall() is False
        future = engine.recall_async("q")
        # The worker is blocked in search() until we release it.
        assert engine.has_pending_recall() is True
        release.set()
        future.result(timeout=5)
        assert engine.has_pending_recall() is False
    finally:
        release.set()
        engine.close()


def test_recall_blank_query_is_safe():
    engine = RecallEngine(Config(), _FakeBackend())
    try:
        result = engine.recall("   ")
        assert result.snippets == []
    finally:
        engine.close()
