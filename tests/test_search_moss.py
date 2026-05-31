"""Tests for the Moss (cloud) search backend.

Exercises the real async->sync bridge in MossBackend against a fake async SDK
inserted into sys.modules, so the coroutine plumbing is genuinely tested (the
fake methods are `async def` and run through run_coroutine_threadsafe).
"""

import sys
import types

import pytest

from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource, get_backend
from observational_memory.search.moss import MossBackend
from observational_memory.search.none import NoneBackend

# --- Fake async Moss SDK -------------------------------------------------


class _DocumentInfo:
    def __init__(self, id, text, metadata=None, embedding=None):
        self.id = id
        self.text = text
        self.metadata = metadata
        self.embedding = embedding


class _QueryOptions:
    def __init__(self, embedding=None, top_k=None, alpha=None, filter=None):
        self.embedding = embedding
        self.top_k = top_k
        self.alpha = alpha
        self.filter = filter


class _MutationOptions:
    def __init__(self, upsert=None):
        self.upsert = upsert


class _Hit:
    def __init__(self, id, text, metadata, score):
        self.id = id
        self.text = text
        self.metadata = metadata
        self.score = score
        self.index_name = None


class _SearchResult:
    def __init__(self, docs):
        self.docs = docs


class _FakeMossClient:
    instances: list["_FakeMossClient"] = []

    def __init__(self, project_id, project_key):
        self.project_id = project_id
        self.project_key = project_key
        self.indexes: dict[str, list[_DocumentInfo]] = {}
        self.loaded: set[str] = set()
        self.created_model_id = None
        self.calls: list[str] = []
        _FakeMossClient.instances.append(self)

    async def delete_index(self, name):
        self.indexes.pop(name, None)
        self.loaded.discard(name)
        return True

    async def create_index(self, name, docs, model_id=None):
        self.calls.append("create_index")
        self.indexes[name] = list(docs)
        self.created_model_id = model_id
        return object()

    async def add_docs(self, name, docs, options=None):
        # Faithful to the real SDK: add_docs requires an existing index.
        if name not in self.indexes:
            raise RuntimeError(f"index '{name}' not found")
        self.calls.append("add_docs")
        by_id = {d.id: d for d in self.indexes[name]}
        for d in docs:  # upsert by id
            by_id[d.id] = d
        self.indexes[name] = list(by_id.values())
        return object()

    async def get_docs(self, name, options=None):
        # Faithful to the real SDK: get_docs requires an existing index.
        if name not in self.indexes:
            raise RuntimeError(f"index '{name}' not found")
        self.calls.append("get_docs")
        return list(self.indexes[name])

    async def delete_docs(self, name, doc_ids):
        self.calls.append("delete_docs")
        drop = set(doc_ids)
        self.indexes[name] = [d for d in self.indexes.get(name, []) if d.id not in drop]
        return object()

    async def load_index(self, name, auto_refresh=False, polling_interval_in_seconds=600):
        if name not in self.indexes:
            raise RuntimeError(f"index '{name}' not found")
        self.loaded.add(name)
        return name

    async def query(self, name, query, options=None):
        docs = self.indexes.get(name, [])
        tokens = query.lower().split()
        hits = [_Hit(d.id, d.text, d.metadata, 1.0) for d in docs if any(tok in d.text.lower() for tok in tokens)]
        top_k = getattr(options, "top_k", None) or 10
        return _SearchResult(hits[:top_k])


@pytest.fixture
def fake_moss(monkeypatch):
    _FakeMossClient.instances = []
    module = types.ModuleType("moss")
    module.MossClient = _FakeMossClient
    module.DocumentInfo = _DocumentInfo
    module.QueryOptions = _QueryOptions
    module.MutationOptions = _MutationOptions
    monkeypatch.setitem(sys.modules, "moss", module)
    yield module


def _docs():
    return [
        Document(
            doc_id="ref:active-projects",
            source=DocumentSource.REFLECTIONS,
            heading="## Active Projects",
            content="## Active Projects\nWorking on the observational memory voice feature.",
            metadata={"source_path": "/m/reflections.md", "source_start_line": 12},
        ),
        Document(
            doc_id="obs:2026-05-30",
            source=DocumentSource.OBSERVATIONS,
            heading="## 2026-05-30",
            content="## 2026-05-30\nDiscussed PostgreSQL indexing performance.",
            date="2026-05-30",
        ),
    ]


def _backend():
    return MossBackend(project_id="pid", project_key="pkey", index_name="om-test")


# --- Tests ---------------------------------------------------------------


def test_index_and_search_roundtrip(fake_moss):
    backend = _backend()
    try:
        backend.index(_docs())
        assert backend.is_ready() is True

        results = backend.search("voice feature", limit=5)
        assert len(results) == 1
        hit = results[0]
        assert hit.document.doc_id == "ref:active-projects"
        assert hit.document.source is DocumentSource.REFLECTIONS
        assert hit.document.heading == "## Active Projects"
        assert "voice feature" in hit.document.content
        assert hit.rank == 1
        # int metadata survives the flat-string Moss map as an int (matches bm25/qmd).
        assert hit.document.metadata.get("source_start_line") == 12
    finally:
        backend.close()


def test_index_creates_then_upserts(fake_moss):
    backend = _backend()
    try:
        backend.index(_docs())  # first time: index missing -> create
        backend.index(_docs())  # second time: exists -> upsert in place
        client = _FakeMossClient.instances[-1]
        assert client.calls == ["create_index", "get_docs", "add_docs"]
        # Upsert by id keeps the corpus de-duplicated rather than doubling it.
        assert len(client.indexes["om-test"]) == 2
    finally:
        backend.close()


def test_reindex_deletes_docs_that_became_local_or_removed(fake_moss):
    backend = _backend()
    try:
        backend.index(_docs())  # uploads ref:active-projects + obs:2026-05-30
        client = _FakeMossClient.instances[-1]
        assert {d.id for d in client.indexes["om-test"]} == {"ref:active-projects", "obs:2026-05-30"}

        # obs:2026-05-30 is now wholly scope=local; reindex must remove it from the cloud.
        docs = _docs()
        docs[1] = Document(
            doc_id="obs:2026-05-30",
            source=DocumentSource.OBSERVATIONS,
            heading="## 2026-05-30",
            content="## 2026-05-30\nNow private. <!--om: scope=local-->",
            date="2026-05-30",
        )
        backend.index(docs)
        remaining = {d.id for d in client.indexes["om-test"]}
        assert remaining == {"ref:active-projects"}
        assert "delete_docs" in client.calls
    finally:
        backend.close()


def test_reindex_to_all_local_empties_cloud_index(fake_moss):
    backend = _backend()
    try:
        backend.index(_docs())
        client = _FakeMossClient.instances[-1]
        # Every section becomes wholly scope=local -> nothing uploadable -> all deleted.
        local_docs = [
            Document(
                doc_id=d.doc_id,
                source=d.source,
                heading=d.heading,
                content=f"{d.heading}\nprivate <!--om: scope=local-->",
                date=d.date,
            )
            for d in _docs()
        ]
        backend.index(local_docs)
        assert client.indexes["om-test"] == []
    finally:
        backend.close()


def test_mixed_scope_section_uploads_shared_lines_only(fake_moss):
    backend = _backend()
    try:
        backend.index(
            [
                Document(
                    doc_id="ref:mixed",
                    source=DocumentSource.REFLECTIONS,
                    heading="## Mixed",
                    content=(
                        "## Mixed\n- Shared fact everyone can see\n- Secret host-only note <!--om: scope=local-->\n"
                    ),
                )
            ]
        )
        client = _FakeMossClient.instances[-1]
        uploaded = {d.id: d.text for d in client.indexes["om-test"]}
        assert "ref:mixed" in uploaded
        assert "Shared fact" in uploaded["ref:mixed"]
        assert "Secret host-only note" not in uploaded["ref:mixed"]
    finally:
        backend.close()


def test_close_is_final_no_resurrection(fake_moss):
    backend = _backend()
    backend.index(_docs())
    backend.close()
    # After close the loop must not be resurrected; calls fail closed instead.
    assert backend.is_ready() is False
    assert backend.search("voice", limit=3) == []


def test_search_reconstructs_observation_source_and_date(fake_moss):
    backend = _backend()
    try:
        backend.index(_docs())
        results = backend.search("postgresql", limit=5)
        assert len(results) == 1
        assert results[0].document.source is DocumentSource.OBSERVATIONS
        assert results[0].document.date == "2026-05-30"
    finally:
        backend.close()


def test_is_ready_false_when_index_never_built(fake_moss):
    backend = _backend()
    try:
        # No index() call -> load_index raises -> fail closed.
        assert backend.is_ready() is False
        assert backend.search("anything", limit=3) == []
    finally:
        backend.close()


def test_scope_local_sections_are_not_uploaded(fake_moss):
    backend = _backend()
    try:
        docs = _docs()
        docs.append(
            Document(
                doc_id="ref:secret",
                source=DocumentSource.REFLECTIONS,
                heading="## Secret",
                content="## Secret\nPrivate note. <!--om: scope=local-->",
            )
        )
        backend.index(docs)
        client = _FakeMossClient.instances[-1]
        uploaded_ids = {d.id for d in client.indexes["om-test"]}
        assert "ref:secret" not in uploaded_ids
        assert "ref:active-projects" in uploaded_ids
    finally:
        backend.close()


def test_fail_closed_when_sdk_missing(monkeypatch):
    # No fake_moss fixture: ensure `import moss` fails.
    monkeypatch.setitem(sys.modules, "moss", None)
    backend = _backend()
    try:
        assert backend.is_ready() is False
        assert backend.search("x", limit=3) == []
        backend.index(_docs())  # no-op, must not raise
    finally:
        backend.close()


def test_upload_notice_printed_once(fake_moss, capsys):
    backend = _backend()
    try:
        backend.index(_docs())
        backend.index(_docs())
        err = capsys.readouterr().err
        assert err.count("Moss cloud") == 1
        assert "service.usemoss.dev" in err
    finally:
        backend.close()


def test_get_backend_moss_without_creds_is_none_backend(monkeypatch):
    monkeypatch.delenv("OM_MOSS_PROJECT_ID", raising=False)
    monkeypatch.delenv("OM_MOSS_PROJECT_KEY", raising=False)
    config = Config()
    config.search_backend = "moss"
    assert isinstance(get_backend("moss", config), NoneBackend)


def test_get_backend_moss_with_creds_is_moss_backend(monkeypatch, fake_moss):
    monkeypatch.setenv("OM_MOSS_PROJECT_ID", "pid")
    monkeypatch.setenv("OM_MOSS_PROJECT_KEY", "pkey")
    config = Config()
    backend = get_backend("moss", config)
    try:
        assert isinstance(backend, MossBackend)
    finally:
        backend.close()
