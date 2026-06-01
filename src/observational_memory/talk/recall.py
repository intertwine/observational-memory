"""Background recall over memories for the talk loop.

`RecallEngine` runs a search-backend query off the conversation's critical path
(on a single background daemon thread) and shapes the hits into trimmed, deduped
snippets the conversation brain can ground a reply in. It degrades cleanly: if
the backend is not ready (e.g. never indexed) it returns an empty, ungrounded
result rather than raising — the conversation keeps working.

The background work runs on a *daemon* thread on purpose: a slow or hung backend
search (a cold qmd load, a wedged Moss request) is abandoned when the turn times
out, and a daemon thread is killed at interpreter exit rather than joined — so a
stuck search can never keep `om talk` from exiting.
"""

from __future__ import annotations

import enum
import logging
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

_DEFAULT_SNIPPET_MAX_CHARS = 600
_DEFAULT_LIMIT = 5


class RecallStatus(str, enum.Enum):
    """The category of a recall outcome, distinct from whether it was grounded.

    - OK:          backend ran and returned at least one snippet.
    - EMPTY:       backend ran successfully but matched nothing for this query.
    - TIMEOUT:     recall did not finish within the caller's time budget.
    - UNAVAILABLE: the search backend is not ready (never indexed, failed to load).
    """

    OK = "ok"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RecallSnippet:
    """One trimmed memory hit suitable for grounding a spoken reply."""

    doc_id: str
    heading: str
    content: str
    source: str
    score: float


@dataclass(frozen=True)
class RecallResult:
    """The outcome of a single recall over memory."""

    query: str
    snippets: list[RecallSnippet] = field(default_factory=list)
    backend_ready: bool = False
    status: RecallStatus = RecallStatus.UNAVAILABLE

    @property
    def grounded(self) -> bool:
        return bool(self.snippets)


class RecallEngine:
    """Run recall over memories, on demand or in the background."""

    def __init__(
        self,
        config,
        backend=None,
        *,
        snippet_max_chars: int = _DEFAULT_SNIPPET_MAX_CHARS,
    ) -> None:
        self._config = config
        self._backend = backend
        self._snippet_max_chars = snippet_max_chars
        # The single in-flight future, tracked so a wedged recall does not silently
        # block (and mislabel) the next turn — see recall_async / has_pending_recall.
        self._inflight: Future | None = None

    def backend(self):
        """Resolve (and cache) the configured search backend."""
        if self._backend is None:
            from ..search import get_backend

            self._backend = get_backend(self._config.search_backend, self._config)
        return self._backend

    def is_ready(self) -> bool:
        try:
            return bool(self.backend().is_ready())
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.debug("recall backend is_ready failed: %s", exc)
            return False

    def recall(self, query: str, limit: int = _DEFAULT_LIMIT) -> RecallResult:
        """Run one recall synchronously. Never raises."""
        query = (query or "").strip()
        if not query:
            ready = self.is_ready()
            return RecallResult(
                query=query,
                backend_ready=ready,
                status=RecallStatus.EMPTY if ready else RecallStatus.UNAVAILABLE,
            )

        backend = self.backend()
        try:
            ready = bool(backend.is_ready())
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.debug("recall is_ready failed: %s", exc)
            ready = False
        if not ready:
            return RecallResult(query=query, backend_ready=False, status=RecallStatus.UNAVAILABLE)

        try:
            results = backend.search(query, limit=limit)
        except Exception as exc:
            _LOGGER.debug("recall search failed: %s", exc)
            # Backend was ready but the query failed: treat as unavailable, not empty —
            # a failed search is not evidence that memory has nothing relevant.
            return RecallResult(query=query, backend_ready=True, status=RecallStatus.UNAVAILABLE)

        snippets = self._shape(results)
        return RecallResult(
            query=query,
            snippets=snippets,
            backend_ready=True,
            status=RecallStatus.OK if snippets else RecallStatus.EMPTY,
        )

    def has_pending_recall(self) -> bool:
        """True if a prior recall is still running on the background thread.

        A daemon thread running ``recall`` cannot be cancelled once started, so a
        recall that overran the caller's budget keeps running. Callers check this
        before submitting so they can classify the next turn as UNAVAILABLE rather
        than spawning a second thread behind the wedged one and falsely reporting a
        TIMEOUT (head-of-line blocking).
        """
        inflight = self._inflight
        return inflight is not None and not inflight.done()

    def recall_async(self, query: str, limit: int = _DEFAULT_LIMIT) -> Future:
        """Run recall on a background *daemon* thread, returning its ``Future``.

        At most one recall is in flight at a time; call ``has_pending_recall``
        first to avoid spawning behind a wedged prior recall. The thread is a
        daemon so a hung backend search is abandoned (not joined) at interpreter
        exit and can never keep ``om talk`` from exiting.
        """
        future: Future = Future()

        def _run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(self.recall(query, limit))
            except BaseException as exc:  # recall() shouldn't raise, but never wedge the Future
                future.set_exception(exc)

        self._inflight = future
        threading.Thread(target=_run, name="om-recall", daemon=True).start()
        return future

    def close(self) -> None:
        # Nothing to join: the recall thread is a daemon and is abandoned on a
        # timed-out turn, so it cannot block process exit. Just drop the handle.
        self._inflight = None

    # -- internals --------------------------------------------------

    def _shape(self, results) -> list[RecallSnippet]:
        snippets: list[RecallSnippet] = []
        seen: set[str] = set()
        for result in results:
            doc = result.document
            if doc.doc_id and doc.doc_id in seen:
                continue
            if doc.doc_id:
                seen.add(doc.doc_id)
            snippets.append(
                RecallSnippet(
                    doc_id=doc.doc_id,
                    heading=doc.heading,
                    content=self._trim(doc.content),
                    source=doc.source.value if hasattr(doc.source, "value") else str(doc.source),
                    score=float(result.score),
                )
            )
        return snippets

    def _trim(self, content: str) -> str:
        content = (content or "").strip()
        if len(content) <= self._snippet_max_chars:
            return content
        return content[: self._snippet_max_chars].rstrip() + " …"
