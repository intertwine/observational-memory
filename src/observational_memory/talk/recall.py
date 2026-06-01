"""Background recall over memories for the talk loop.

`RecallEngine` runs a search-backend query off the conversation's critical path
(on a single-worker executor) and shapes the hits into trimmed, deduped snippets
the conversation brain can ground a reply in. It degrades cleanly: if the
backend is not ready (e.g. never indexed) it returns an empty, ungrounded result
rather than raising — the conversation keeps working.
"""

from __future__ import annotations

import enum
import logging
from concurrent.futures import Future, ThreadPoolExecutor
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
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="om-recall")
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
        """True if a prior recall is still occupying the single worker.

        The executor has one worker and ``ThreadPoolExecutor`` cannot cancel an
        already-running task, so a recall that overran the caller's budget keeps
        running. Callers check this before submitting so they can classify the
        next turn as UNAVAILABLE rather than queueing behind the wedged task and
        falsely reporting a TIMEOUT (head-of-line blocking).
        """
        inflight = self._inflight
        return inflight is not None and not inflight.done()

    def recall_async(self, query: str, limit: int = _DEFAULT_LIMIT) -> Future:
        """Submit recall to the background executor.

        At most one recall is in flight at a time (single worker). A previously
        submitted future that has already finished is cleared here; a still-running
        one is left in place so the caller (via ``has_pending_recall``) can avoid
        queueing behind it. Use ``has_pending_recall`` to detect a wedged prior
        recall before calling this.
        """
        self._inflight = self._executor.submit(self.recall, query, limit)
        return self._inflight

    def close(self) -> None:
        self._executor.shutdown(wait=False)
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
