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
        self._closed = False

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                # close() is final: never resurrect a torn-down loop/thread.
                raise RuntimeError("async loop is closed")
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
        """Run *coro* on the background loop and block for its result.

        On timeout the underlying coroutine is asked to cancel (best-effort —
        it may already be inside a blocking ``asyncio.to_thread`` call) so a slow
        upload/query does not pin the single background loop indefinitely.
        """
        try:
            loop = self._ensure()
        except RuntimeError:
            coro.close()  # avoid "coroutine was never awaited" when the loop is closed
            raise
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._loop is None:
                return
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            self._loop = None
            self._thread = None


def _strip_local_lines(content: str) -> str:
    """Drop ``scope=local`` lines from a section before upload.

    Line-based, matching ``filter_reflection_entries_for_cluster`` exactly, so a
    section that mixes shared and host-local entries still contributes its shared
    entries to the cloud index (rather than being dropped wholesale) while local
    entries never leave the host.
    """
    try:
        from ..reflection_metadata import parse_metadata
    except Exception:  # pragma: no cover - defensive
        return content
    kept = [line for line in content.splitlines() if parse_metadata(line).get("scope") != "local"]
    return "\n".join(kept)


def _has_indexable_body(content: str) -> bool:
    """True if a section has any non-heading, non-blank line worth uploading."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
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
        """Upsert *documents* into the Moss index, withholding scope=local content.

        Uploads memory text to the Moss cloud. ``scope=local`` lines are stripped
        per line (sections that mix shared and local entries still upload their
        shared entries). Fails closed: any SDK/network error is logged at debug
        level and swallowed.

        Note on staleness: this upserts the current corpus rather than deleting
        and recreating, so it never leaves the index empty on a partial failure.
        The trade-off is that sections removed from memory since the last index
        linger in the cloud index until the next successful full reindex of a
        renamed index; recall still surfaces current content first by relevance.
        """
        client = self._ensure_client()
        if client is None:
            return

        uploadable: list[Document] = []
        dropped = 0
        for doc in documents:
            stripped = _strip_local_lines(doc.content)
            if _has_indexable_body(stripped):
                uploadable.append(_with_content(doc, stripped))
            else:
                dropped += 1
        if not uploadable:
            _LOGGER.debug("moss index: nothing to upload after scope=local filtering")
            return

        self._announce_upload(len(uploadable), dropped=dropped)

        try:
            from moss import DocumentInfo, MutationOptions  # type: ignore
        except Exception:  # pragma: no cover - covered by _ensure_client
            return

        infos = [
            DocumentInfo(id=doc.doc_id, text=doc.content, metadata=self._encode_metadata(doc)) for doc in uploadable
        ]

        async def _replace() -> None:
            # Upsert in place when the index exists; create it on first use. Never
            # delete-then-create — that would leave no index if create failed.
            try:
                await client.add_docs(self._index_name, infos, MutationOptions(upsert=True))
            except Exception:
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
            # auto_refresh stays off: one CLI invocation loads once. index() resets
            # _loaded so a same-process reindex is picked up; a corpus changed by
            # another process is intentionally not hot-reloaded mid-session.
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
            metadata = _restore_metadata_types(dict(getattr(hit, "metadata", None) or {}))
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


def _with_content(doc: Document, content: str) -> Document:
    """A copy of *doc* with replaced content (used after stripping local lines)."""
    return Document(
        doc_id=doc.doc_id,
        source=doc.source,
        heading=doc.heading,
        content=content,
        date=doc.date,
        metadata=doc.metadata,
    )


# Integer metadata fields that were stringified for Moss's flat string map; these
# are restored to int on read so Moss hits match bm25/qmd shape (e.g. for
# `_format_location` in the recall command).
_INT_METADATA_KEYS = ("source_line", "source_start_line")


def _restore_metadata_types(metadata: dict) -> dict:
    for key in _INT_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.lstrip("-").isdigit():
            metadata[key] = int(value)
    return metadata


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
