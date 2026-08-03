from __future__ import annotations

import json
import threading
import time

import pytest

import observational_memory.search as search_module
from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource, get_backend, reindex
from observational_memory.search.generation import DocumentBatch, canonical_json_bytes
from observational_memory.search.generation_store import GenerationStoreError, StoreTransaction
from observational_memory.search.parser import parse_observations, parse_reflections


def test_document_batch_is_backed_only_by_immutable_bytes():
    document = Document(
        doc_id="one",
        source=DocumentSource.OBSERVATIONS,
        heading="one",
        content="captured",
        metadata={"key": "captured"},
    )
    batch = DocumentBatch.create(
        [document],
        backend_name="bm25",
        backend_config_digest="digest",
    )

    document.content = "mutated"
    document.metadata["key"] = "mutated"
    first_view = batch.documents[0]
    first_view.content = "mutated view"
    first_view.metadata["key"] = "mutated view"

    second_view = batch.documents[0]
    assert second_view.content == "captured"
    assert second_view.metadata["key"] == "captured"


def test_document_batch_rejects_mixed_generation_documents():
    documents = []
    for doc_id, source, generation_id in (
        ("one", DocumentSource.OBSERVATIONS, "a"),
        ("two", DocumentSource.REFLECTIONS, "b"),
    ):
        documents.append(
            canonical_json_bytes(
                {
                    "doc_id": doc_id,
                    "source": source.value,
                    "heading": doc_id,
                    "content": doc_id,
                    "date": None,
                    "metadata": {"generation_id": generation_id},
                    "owner": None,
                    "scope": None,
                    "source_type": None,
                }
            )
        )
    with pytest.raises(ValueError, match="more than one generation"):
        DocumentBatch(
            generation_id="a",
            document_bytes=tuple(documents),
            backend_name="bm25",
            backend_config_digest="digest",
        )


def test_ordinary_reindex_waits_for_complete_materialization_generation(tmp_path):
    """Invariant: ordinary capture waits for the one root-owned materialization transaction."""
    projects = tmp_path / "projects"
    projects.mkdir()
    config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=projects, search_backend="bm25")
    config.memory_dir.mkdir()
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\nold observation")
    config.reflections_path.write_text("# Reflections\n\n## Core\n\nold reflection")
    result: list[int] = []

    with StoreTransaction.acquire(config.search_index_dir):
        thread = threading.Thread(target=lambda: result.append(reindex(config)))
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()
        config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\nnew observation")
        config.reflections_path.write_text("# Reflections\n\n## Core\n\nnew reflection")
    thread.join(2)

    assert result == [2]
    backend = get_backend("bm25", config)
    corpus = "\n".join(document.content for document in backend._documents)
    assert "new observation" in corpus
    assert "new reflection" in corpus
    assert "old observation" not in corpus
    assert "old reflection" not in corpus
    assert {document.metadata["generation_id"] for document in backend._documents} == {backend.committed_generation_id}


def test_ordinary_reindex_keeps_legacy_claude_auto_memory(tmp_path):
    projects = tmp_path / "projects"
    memory = projects / "project-a" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Legacy Claude memory\n\nDo not suppress this.")
    config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=projects, search_backend="bm25")
    config.memory_dir.mkdir()

    assert reindex(config) == 1

    backend = get_backend("bm25", config)
    assert [document.source for document in backend._documents] == [DocumentSource.AUTO_MEMORY]


@pytest.mark.parametrize("backend_name", ["qmd", "qmd-hybrid", "moss", "none"])
def test_non_bm25_reindex_preserves_legacy_payload_and_creates_no_generation_store(
    tmp_path,
    monkeypatch,
    backend_name,
):
    """Invariant: excluded backends receive base documents and no generation artifacts."""
    projects = tmp_path / "projects"
    projects.mkdir()
    config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=projects, search_backend=backend_name)
    config.memory_dir.mkdir()
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\nlegacy observation")
    config.reflections_path.write_text("# Reflections\n\n## Core\n\nlegacy reflection")
    expected = parse_observations(config.observations_path) + parse_reflections(config.reflections_path)
    indexed: list[Document] = []

    class FakeBackend:
        def index(self, documents):
            indexed.extend(documents)

    monkeypatch.setattr(search_module, "get_backend", lambda _name, _config: FakeBackend())

    assert reindex(config) == 2
    assert [(doc.doc_id, doc.metadata) for doc in indexed] == [(doc.doc_id, doc.metadata) for doc in expected]
    assert all("generation_id" not in document.metadata for document in indexed)
    assert not config.search_index_dir.exists()


def test_persisted_authority_rejects_tampered_index_and_accepts_only_a_new_verified_generation(tmp_path):
    """Invariant: a fresh reader, not committing memory, is commit authority."""
    projects = tmp_path / "projects"
    projects.mkdir()
    config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=projects, search_backend="bm25")
    config.memory_dir.mkdir()
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ncanonical observation")
    assert reindex(config) == 1
    pointer = json.loads((config.search_index_dir / "current-generation.json").read_text())
    index_path = config.search_index_dir / "generations" / pointer["generation_id"] / "index.json"
    forged = json.loads(index_path.read_text())
    forged["documents"][0]["content"] = "forged observation"
    forged["tokenized_corpus"][0] = ["forged", "observation"]
    index_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    index_path.chmod(0o600)

    with pytest.raises(GenerationStoreError):
        get_backend("bm25", config)

    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ncanonical replacement")
    assert reindex(config) == 1
    repaired = get_backend("bm25", config)
    assert repaired.committed_generation_digest is not None
    assert "canonical replacement" in repaired._documents[0].content
    assert "forged observation" not in repaired._documents[0].content
