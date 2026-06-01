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
    """Drop non-shareable content from a section before upload.

    Routes its keep-decision through the SAME ``_scope_is_shareable`` allowlist
    resolver as ``filter_reflection_entries_for_cluster`` (no longer the inline
    ``!= "local"`` check), so the two leak-critical share-out paths have a SINGLE
    source of truth and cannot drift: a section that mixes shared and host-local
    entries still contributes its shared entries to the cloud index, while a line
    carrying any EXPLICIT non-shareable scope — ``scope=local`` or, newly closed
    in Gate 4, a typo / hallucinated / future / hand-typed ``team``/``org`` value
    — never leaves the host. Absent-scope lines ride along, exactly as today.

    LEAK-CRITICAL: after dropping non-shareable entries this also prunes any
    now-empty heading block at ANY level via ``_drop_empty_heading_sections`` — so
    a private H3/H4 subsection whose every bullet was withheld does NOT leak its
    title into the uploaded text just because some *other* subsection of the same
    H2 is shared (PR #85 re-review P1). A withheld multi-line bullet also drops its
    indented continuation lines via ``_shareable_lines`` so wrapped continuation
    text never leaks (PR #86 re-review P1). A line-only strip left both behind.
    """
    try:
        from ..reflection_metadata import _drop_empty_heading_sections, _shareable_lines
    except Exception:  # pragma: no cover - defensive
        return content
    kept = _shareable_lines(content.splitlines())
    kept = _drop_empty_heading_sections(kept)
    return "\n".join(kept)


def _has_indexable_body(content: str) -> bool:
    """True if a section has any non-heading, non-blank line worth uploading.

    LEAK-CRITICAL: an HTML-comment line (e.g. the Gate 3
    ``<!--om-section: ...-->`` provenance stamp) is NOT real body. A section
    whose every bullet was ``scope=local`` strips down to just its heading plus
    the stamp; without skipping comment lines the stamp would make the section
    look indexable and leak the heading + cadence to the cloud. Treat any
    ``<!--`` line as empty so such a section is still withheld, exactly as it was
    pre-stamp.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
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
        """Reconcile the Moss index to *documents*, withholding scope=local content.

        Uploads memory text to the Moss cloud. ``scope=local`` lines are stripped
        per line (sections that mix shared and local entries still upload their
        shared entries; a section that is wholly local is excluded entirely).

        Reconciles rather than blindly upserts: the current docs are upserted and
        any cloud doc no longer present locally — because it was removed, or has
        become wholly ``scope=local`` — is **deleted**, so stale or now-private
        memory cannot linger in the cloud index and be returned by recall. This
        order (upsert-then-delete, never delete-then-create) also means a partial
        failure never leaves the index empty. Fails closed: any SDK/network error
        is logged at debug level and swallowed.
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

        if uploadable:
            self._announce_upload(len(uploadable), dropped=dropped)

        try:
            from moss import DocumentInfo, MutationOptions  # type: ignore
        except Exception:  # pragma: no cover - covered by _ensure_client
            return

        infos = [
            DocumentInfo(id=doc.doc_id, text=doc.content, metadata=self._encode_metadata(doc)) for doc in uploadable
        ]
        keep_ids = {doc.doc_id for doc in uploadable}

        async def _reconcile() -> None:
            # get_docs both lists current cloud docs and tells us whether the index
            # exists (it raises when it doesn't). Best-effort: if the SDK paginates
            # get_docs, any stale doc missed this round is reconciled next reindex.
            try:
                existing = await client.get_docs(self._index_name)
            except Exception:
                existing = None
            if existing is None:
                # No index yet — create it from the current docs (nothing to delete).
                if infos:
                    await client.create_index(self._index_name, infos, self._model_id)
                return
            if infos:
                await client.add_docs(self._index_name, infos, MutationOptions(upsert=True))
            stale = [
                doc_id for doc_id in (getattr(d, "id", None) for d in existing) if doc_id and doc_id not in keep_ids
            ]
            if stale:
                await client.delete_docs(self._index_name, stale)

        try:
            self._loop.run(_reconcile(), _INDEX_TIMEOUT)
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
            _LOGGER.debug("moss load_index failed (is the index built? run `om talk --reindex`): %s", exc)
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
        # Gate 3 typed provenance: encode owner/source_type ONLY, never `scope`.
        # LEAK-CRITICAL: `_encode_metadata` writes a cloud flat-string map that
        # bypasses the per-line `_strip_local_lines` gate, so a `scope` value here
        # could re-reveal that a section carried local material. owner/source_type
        # are safe because the uploadable Document is `_with_content(doc, stripped)`
        # and these fields are RE-DERIVED there from the local-stripped content.
        if doc.source_type:
            meta["source_type"] = doc.source_type
        if doc.owner:
            meta["owner"] = doc.owner
        return meta

    def _map_results(self, result: object) -> list[SearchResult]:
        docs = getattr(result, "docs", None) or []
        out: list[SearchResult] = []
        for rank, hit in enumerate(docs, start=1):
            doc_id = str(getattr(hit, "id", "") or "")
            metadata = _restore_metadata_types(dict(getattr(hit, "metadata", None) or {}))
            heading = str(metadata.get("heading") or "")
            date = metadata.get("date")
            # Gate 3 typed provenance: pop owner/source_type OUT of the metadata
            # dict so the typed fields are their single home and the metadata dict
            # matches the parser/bm25/qmd shape (which never carries these keys).
            source_type = metadata.pop("source_type", None)
            owner = metadata.pop("owner", None)
            out.append(
                SearchResult(
                    document=Document(
                        doc_id=doc_id,
                        source=_source_from_metadata(metadata, doc_id),
                        heading=heading,
                        content=str(getattr(hit, "text", "") or ""),
                        date=date if isinstance(date, str) else None,
                        metadata=metadata,
                        # scope is never encoded to the cloud, so it comes back
                        # None on read (correct and default-preserving).
                        owner=owner if isinstance(owner, str) else None,
                        source_type=source_type if isinstance(source_type, str) else None,
                    ),
                    score=float(getattr(hit, "score", 0.0) or 0.0),
                    rank=rank,
                )
            )
        return out


def _with_content(doc: Document, content: str) -> Document:
    """A copy of *doc* with replaced content (used after stripping local lines).

    The typed provenance fields are RE-DERIVED from the local-stripped *content*,
    not copied from the pre-strip Document. This is adversarially leak-safe: an
    owner/source_type tied to a stripped-out local fact never reaches the cloud
    map encoded by ``_encode_metadata``. Without this re-derivation the new
    dataclass fields would reset to their defaults on the uploadable copy.
    """
    from .parser import derive_section_provenance

    owner, scope, source_type = derive_section_provenance(content)
    return Document(
        doc_id=doc.doc_id,
        source=doc.source,
        heading=doc.heading,
        content=content,
        date=doc.date,
        metadata=doc.metadata,
        owner=owner,
        scope=scope,
        source_type=source_type,
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
