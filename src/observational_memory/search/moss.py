"""Moss search backend — cloud-backed semantic search (https://www.moss.dev).

Moss is a real-time semantic-search runtime. Indexing UPLOADS document text to
``service.usemoss.dev``; ``load_index`` then downloads the index into memory so
queries run locally in ~1-10 ms. This backend is therefore **opt-in only**
(``OM_SEARCH_BACKEND=moss`` plus ``OM_MOSS_PROJECT_ID`` / ``OM_MOSS_PROJECT_KEY``)
and it drops ``scope=local`` memory before upload, mirroring the rule that
host-local entries never become shared cluster memory.

The ``moss`` SDK is async; this backend bridges it onto the synchronous
``SearchBackend`` protocol via one persistent event loop on a daemon thread.
Everything fails closed: a missing package, missing creds, or any SDK/network
error yields ``is_ready() == False`` / an empty result, never an exception into
the caller. The project key is a secret and is never logged or printed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from . import Document, DocumentSource, SearchResult

_LOGGER = logging.getLogger(__name__)

# Default timeouts (seconds). Indexing uploads the whole corpus; loading
# downloads it — both are slower than a query.
_QUERY_TIMEOUT = 30.0
_INDEX_TIMEOUT = 120.0
_LOAD_TIMEOUT = 120.0


class _AsyncLoop:
    """A single persistent asyncio loop running on a daemon thread.

    The Moss client's async methods are run here via ``run_coroutine_threadsafe``
    so the synchronous backend never creates/tears down a loop per call and never
    binds the client to a loop that later closes.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run, args=(loop,), daemon=True, name="om-moss-loop")
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def run(self, coro, timeout: float):
        """Run *coro* on the background loop and block for its result."""
        loop = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._loop is None:
                return
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            self._loop = None
            self._thread = None


def _scope_is_local(content: str) -> bool:
    """True if a memory section is tagged ``scope=local`` and must not be uploaded."""
    try:
        from ..reflection_metadata import parse_metadata
    except Exception:  # pragma: no cover - defensive
        return False
    for line in content.splitlines():
        if parse_metadata(line).get("scope") == "local":
            return True
    return False


class MossBackend:
    """Search backend backed by the cloud Moss semantic-search runtime."""

    def __init__(
        self,
        *,
        project_id: str,
        project_key: str,
        index_name: str = "observational-memory",
        model_id: str | None = None,
        alpha: float | None = None,
    ) -> None:
        self._project_id = project_id
        self._project_key = project_key
        self._index_name = index_name
        self._model_id = model_id
        self._alpha = alpha
        self._loop = _AsyncLoop()
        self._client = None  # lazily created moss.MossClient
        self._client_failed = False
        self._loaded = False  # index downloaded into memory for fast local queries
        self._upload_notice_shown = False

    # -- SDK access -------------------------------------------------

    def _ensure_client(self):
        """Create the MossClient once, or return None if the SDK/creds are unusable."""
        if self._client is not None:
            return self._client
        if self._client_failed:
            return None
        try:
            from moss import MossClient  # type: ignore
        except Exception as exc:
            _LOGGER.debug("moss SDK unavailable: %s", exc)
            self._client_failed = True
            return None
        try:
            self._client = MossClient(self._project_id, self._project_key)
        except Exception as exc:  # pragma: no cover - constructor is cheap, defensive
            _LOGGER.debug("failed to construct MossClient: %s", exc)
            self._client_failed = True
            return None
        return self._client

    # -- SearchBackend protocol ------------------------------------

    def index(self, documents: list[Document]) -> None:
        """Replace the Moss index with *documents*, dropping scope=local content.

        Uploads memory text to the Moss cloud. Fails closed: any SDK/network
        error is logged at debug level and swallowed (the prior index, if any,
        is left untouched on a failed replace attempt).
        """
        client = self._ensure_client()
        if client is None:
            return

        uploadable = [doc for doc in documents if not _scope_is_local(doc.content)]
        if not uploadable:
            _LOGGER.debug("moss index: nothing to upload after scope=local filtering")
            return

        self._announce_upload(len(uploadable), dropped=len(documents) - len(uploadable))

        try:
            from moss import DocumentInfo  # type: ignore
        except Exception:  # pragma: no cover - covered by _ensure_client
            return

        infos = [
            DocumentInfo(id=doc.doc_id, text=doc.content, metadata=self._encode_metadata(doc)) for doc in uploadable
        ]

        async def _replace() -> None:
            # Clean replace to match the protocol's "replacing any previous index"
            # contract. delete_index is best-effort (the index may not exist yet).
            try:
                await client.delete_index(self._index_name)
            except Exception:
                pass
            await client.create_index(self._index_name, infos, self._model_id)

        try:
            self._loop.run(_replace(), _INDEX_TIMEOUT)
            self._loaded = False  # force a reload so queries see the new corpus
        except Exception as exc:
            _LOGGER.debug("moss index failed: %s", exc)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not query:
            return []
        if not self._ensure_loaded():
            return []
        client = self._client
        if client is None:  # pragma: no cover - _ensure_loaded guarantees a client
            return []

        try:
            from moss import QueryOptions  # type: ignore
        except Exception:  # pragma: no cover
            return []

        options_kwargs: dict = {"top_k": limit}
        if self._alpha is not None:
            options_kwargs["alpha"] = self._alpha
        options = QueryOptions(**options_kwargs)

        try:
            result = self._loop.run(client.query(self._index_name, query, options), _QUERY_TIMEOUT)
        except Exception as exc:
            _LOGGER.debug("moss query failed: %s", exc)
            return []

        return self._map_results(result)

    def is_ready(self) -> bool:
        """True only if the SDK and creds are usable and the index can be loaded."""
        return self._ensure_loaded()

    def close(self) -> None:
        self._loop.close()

    # -- internals --------------------------------------------------

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        client = self._ensure_client()
        if client is None:
            return False
        try:
            self._loop.run(client.load_index(self._index_name), _LOAD_TIMEOUT)
        except Exception as exc:
            _LOGGER.debug("moss load_index failed (is the index built? run `om reindex`): %s", exc)
            return False
        self._loaded = True
        return True

    def _announce_upload(self, count: int, *, dropped: int) -> None:
        if self._upload_notice_shown:
            return
        self._upload_notice_shown = True
        suffix = f" ({dropped} scope=local section(s) withheld)" if dropped else ""
        print(
            f"om: uploading {count} memory section(s) to the Moss cloud (service.usemoss.dev){suffix}",
            file=sys.stderr,
        )

    @staticmethod
    def _encode_metadata(doc: Document) -> dict[str, str]:
        """Moss metadata is a flat string map; encode just enough to rebuild Document."""
        meta: dict[str, str] = {"source": doc.source.value, "heading": doc.heading}
        if doc.date:
            meta["date"] = doc.date
        for key in ("source_path", "file_path", "source_line", "source_start_line"):
            value = doc.metadata.get(key)
            if value is not None:
                meta[key] = str(value)
        return meta

    def _map_results(self, result: object) -> list[SearchResult]:
        docs = getattr(result, "docs", None) or []
        out: list[SearchResult] = []
        for rank, hit in enumerate(docs, start=1):
            doc_id = str(getattr(hit, "id", "") or "")
            metadata = dict(getattr(hit, "metadata", None) or {})
            heading = str(metadata.get("heading") or "")
            date = metadata.get("date")
            out.append(
                SearchResult(
                    document=Document(
                        doc_id=doc_id,
                        source=_source_from_metadata(metadata, doc_id),
                        heading=heading,
                        content=str(getattr(hit, "text", "") or ""),
                        date=date if isinstance(date, str) else None,
                        metadata=metadata,
                    ),
                    score=float(getattr(hit, "score", 0.0) or 0.0),
                    rank=rank,
                )
            )
        return out


def _source_from_metadata(metadata: dict, doc_id: str) -> DocumentSource:
    name = metadata.get("source")
    if isinstance(name, str):
        try:
            return DocumentSource(name)
        except ValueError:
            pass
    if doc_id.startswith("obs:"):
        return DocumentSource.OBSERVATIONS
    if doc_id.startswith("amem:"):
        return DocumentSource.AUTO_MEMORY
    return DocumentSource.REFLECTIONS
