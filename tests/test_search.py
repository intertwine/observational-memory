"""Tests for the pluggable search module."""

import json

import pytest

from observational_memory.config import Config
from observational_memory.search import (
    Document,
    DocumentSource,
    get_backend,
    reindex,
)
from observational_memory.search.bm25 import BM25Backend, _tokenize
from observational_memory.search.none import NoneBackend
from observational_memory.search.parser import parse_observations, parse_reflections
from observational_memory.search.qmd import QMDBackend, QMDIndexInfo, inspect_qmd_index, qmd_collection_exists

# --- Fixtures ---

SAMPLE_OBSERVATIONS = """\
# Observations

<!-- Auto-maintained by the Observer. -->

## 2026-02-10

### Current Context
- **Active task:** Building observational memory system

### Observations
- 🔴 22:15 First successful observer run on PostgreSQL transcript
- 🟡 22:16 Agent compressed 1.7MB transcript into prioritized observations

## 2026-02-09

### Current Context
- **Active task:** Setting up project infrastructure

### Observations
- 🔴 14:00 Created initial repo structure
- 🟡 14:30 Added Click CLI framework
"""

SAMPLE_REFLECTIONS = """\
# Reflections — Long-Term Memory

*Last updated: 2026-02-10 08:00 UTC*

## Core Identity
- **Name:** Bryan Young
- **Role:** Software engineer

## Active Projects

### observational-memory
- **Status:** active
- **Stack:** Python, Click CLI

## Preferences & Opinions
- Values structured problem-solving
- Prefers uv over pip
"""


# --- Document Parser Tests ---


class TestParseObservations:
    def test_splits_by_date(self, tmp_path):
        obs_file = tmp_path / "observations.md"
        obs_file.write_text(SAMPLE_OBSERVATIONS)
        docs = parse_observations(obs_file)
        assert len(docs) == 2
        assert docs[0].doc_id == "obs:2026-02-10"
        assert docs[1].doc_id == "obs:2026-02-09"

    def test_documents_have_correct_source(self, tmp_path):
        obs_file = tmp_path / "observations.md"
        obs_file.write_text(SAMPLE_OBSERVATIONS)
        docs = parse_observations(obs_file)
        for doc in docs:
            assert doc.source == DocumentSource.OBSERVATIONS

    def test_documents_contain_content(self, tmp_path):
        obs_file = tmp_path / "observations.md"
        obs_file.write_text(SAMPLE_OBSERVATIONS)
        docs = parse_observations(obs_file)
        assert "PostgreSQL" in docs[0].content
        assert "infrastructure" in docs[1].content

    def test_documents_include_source_metadata(self, tmp_path):
        obs_file = tmp_path / "observations.md"
        obs_file.write_text(SAMPLE_OBSERVATIONS)
        docs = parse_observations(obs_file)

        assert docs[0].metadata["file_path"] == str(obs_file)
        assert docs[0].metadata["source_start_line"] == 5

    def test_empty_file(self, tmp_path):
        obs_file = tmp_path / "observations.md"
        obs_file.write_text("")
        assert parse_observations(obs_file) == []

    def test_missing_file(self, tmp_path):
        assert parse_observations(tmp_path / "missing.md") == []


class TestParseReflections:
    def test_splits_by_section(self, tmp_path):
        ref_file = tmp_path / "reflections.md"
        ref_file.write_text(SAMPLE_REFLECTIONS)
        docs = parse_reflections(ref_file)
        assert len(docs) == 3
        assert docs[0].doc_id == "ref:core-identity"
        assert docs[1].doc_id == "ref:active-projects"
        assert docs[2].doc_id == "ref:preferences-opinions"

    def test_documents_have_correct_source(self, tmp_path):
        ref_file = tmp_path / "reflections.md"
        ref_file.write_text(SAMPLE_REFLECTIONS)
        docs = parse_reflections(ref_file)
        for doc in docs:
            assert doc.source == DocumentSource.REFLECTIONS

    def test_documents_include_source_metadata(self, tmp_path):
        ref_file = tmp_path / "reflections.md"
        ref_file.write_text(SAMPLE_REFLECTIONS)
        docs = parse_reflections(ref_file)

        assert docs[0].metadata["file_path"] == str(ref_file)
        assert docs[0].metadata["source_start_line"] == 5

    def test_empty_file(self, tmp_path):
        ref_file = tmp_path / "reflections.md"
        ref_file.write_text("")
        assert parse_reflections(ref_file) == []

    def test_missing_file(self, tmp_path):
        assert parse_reflections(tmp_path / "missing.md") == []


# --- BM25 Backend Tests ---


class TestTokenize:
    def test_lowercases(self):
        assert "hello" in _tokenize("Hello World")

    def test_strips_markdown(self):
        tokens = _tokenize("## PostgreSQL **setup**")
        assert "postgresql" in tokens
        assert "setup" in tokens
        assert "##" not in tokens

    def test_removes_stopwords(self):
        tokens = _tokenize("the quick brown fox and the lazy dog")
        assert "the" not in tokens
        assert "and" not in tokens
        assert "quick" in tokens


class TestBM25Backend:
    def _make_docs(self):
        return [
            Document(
                doc_id="obs:2026-02-10",
                source=DocumentSource.OBSERVATIONS,
                heading="## 2026-02-10",
                content="Setting up PostgreSQL database for project Atlas with connection pooling",
            ),
            Document(
                doc_id="obs:2026-02-09",
                source=DocumentSource.OBSERVATIONS,
                heading="## 2026-02-09",
                content="Created React dashboard with TypeScript for monitoring trades",
            ),
            Document(
                doc_id="ref:preferences",
                source=DocumentSource.REFLECTIONS,
                heading="## Preferences",
                content="Prefers PostgreSQL over SQLite for production workloads",
            ),
        ]

    def test_index_and_search(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        backend.index(self._make_docs())

        results = backend.search("PostgreSQL")
        assert len(results) >= 1
        # PostgreSQL appears in two docs
        doc_ids = [r.document.doc_id for r in results]
        assert "obs:2026-02-10" in doc_ids

    def test_is_ready_after_index(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        assert not backend.is_ready()

        backend.index(self._make_docs())
        assert backend.is_ready()

    def test_persistence(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend1 = BM25Backend(index_path)
        backend1.index(self._make_docs())

        # Create a new instance — should load from pickle
        backend2 = BM25Backend(index_path)
        assert backend2.is_ready()
        results = backend2.search("PostgreSQL")
        assert len(results) >= 1

    def test_zero_score_filtering(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        backend.index(self._make_docs())

        results = backend.search("xyznonexistent")
        assert len(results) == 0

    def test_search_ranking(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        backend.index(self._make_docs())

        results = backend.search("PostgreSQL database")
        assert len(results) >= 1
        # First result should be the one with both keywords
        assert results[0].document.doc_id == "obs:2026-02-10"
        assert results[0].rank == 1

    def test_search_falls_back_for_zero_idf_common_terms(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        backend.index(
            [
                Document(
                    doc_id="obs:2026-04-06",
                    source=DocumentSource.OBSERVATIONS,
                    heading="## 2026-04-06",
                    content="launchd remains the preferred scheduler on macOS",
                ),
                Document(
                    doc_id="obs:2026-04-05",
                    source=DocumentSource.OBSERVATIONS,
                    heading="## 2026-04-05",
                    content="green means green before merge",
                ),
                Document(
                    doc_id="ref:projects",
                    source=DocumentSource.REFLECTIONS,
                    heading="## Projects",
                    content="shipping qmd integration slices",
                ),
                Document(
                    doc_id="ref:preferences",
                    source=DocumentSource.REFLECTIONS,
                    heading="## Preferences",
                    content="prefer launchd over cron on macOS",
                ),
            ]
        )

        results = backend.search("launchd")

        assert [r.document.doc_id for r in results] == [
            "obs:2026-04-06",
            "ref:preferences",
        ]
        assert all(r.score > 0 for r in results)

    def test_empty_query(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        backend.index(self._make_docs())

        results = backend.search("")
        assert results == []

    def test_search_not_ready(self, tmp_path):
        index_path = tmp_path / "bm25.pkl"
        backend = BM25Backend(index_path)
        results = backend.search("anything")
        assert results == []


# --- None Backend Tests ---


class TestNoneBackend:
    def test_search_returns_empty(self):
        backend = NoneBackend()
        assert backend.search("anything") == []

    def test_is_ready_false(self):
        backend = NoneBackend()
        assert not backend.is_ready()

    def test_index_noop(self):
        backend = NoneBackend()
        backend.index([])  # Should not raise


# --- Factory Tests ---


class TestGetBackend:
    def test_bm25(self, tmp_path):
        config = Config(memory_dir=tmp_path)
        backend = get_backend("bm25", config)
        assert isinstance(backend, BM25Backend)

    def test_none(self, tmp_path):
        config = Config(memory_dir=tmp_path)
        backend = get_backend("none", config)
        assert isinstance(backend, NoneBackend)

    def test_unknown_raises(self, tmp_path):
        config = Config(memory_dir=tmp_path)
        with pytest.raises(ValueError, match="Unknown search backend"):
            get_backend("invalid", config)

    def test_qmd(self, tmp_path):
        config = Config(memory_dir=tmp_path, qmd_index_name="om-review")
        backend = get_backend("qmd", config)
        assert isinstance(backend, QMDBackend)
        assert backend._mode == "search"
        assert backend._index_name == "om-review"

    def test_qmd_hybrid(self, tmp_path):
        config = Config(memory_dir=tmp_path, qmd_index_name="om-review", qmd_no_rerank=True)
        backend = get_backend("qmd-hybrid", config)
        assert isinstance(backend, QMDBackend)
        assert backend._mode == "query"
        assert backend._index_name == "om-review"
        assert backend._no_rerank is True


class TestQMDBackend:
    def test_query_uses_index_and_no_rerank(self, tmp_path, monkeypatch):
        calls = []
        backend = QMDBackend(
            tmp_path,
            mode="query",
            index_name="om-review",
            no_rerank=True,
            model_env={"QMD_EMBED_MODEL": "embed-model"},
        )
        encoded_filename = backend._filename_for_doc_id("obs:2026-02-10")

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args == ["qmd", "--help"]:
                return Result(stdout="--index\n--no-rerank\nqmd bench")
            if args == [
                "qmd",
                "--index",
                "om-review",
                "query",
                "lex: launchd\nvec: launchd",
                "-c",
                "observational-memory",
                "-n",
                "10",
                "--json",
                "--no-rerank",
            ]:
                return Result(
                    stdout=json.dumps(
                        [
                            {
                                "docid": "#abc123",
                                "file": f"qmd://observational-memory/{encoded_filename}",
                                "title": "## 2026-02-10",
                                "snippet": "launchd migration",
                                "line": 12,
                            }
                        ]
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("shutil.which", lambda name: "/tmp/bin/qmd" if name == "qmd" else None)
        monkeypatch.setattr("subprocess.run", fake_run)

        results = backend.search("launchd")

        assert len(results) == 1
        assert results[0].document.doc_id == "obs:2026-02-10"
        assert results[0].document.metadata["qmd_docid"] == "#abc123"
        assert results[0].document.metadata["line"] == 12
        assert results[0].document.metadata["qmd_line"] == 12
        query_call = next(call for call in calls if "query" in call[0])
        assert query_call[0][4] == "lex: launchd\nvec: launchd"
        assert any(call[0][-1] == "--no-rerank" for call in calls if "query" in call[0])
        assert query_call[1]["env"]["QMD_EMBED_MODEL"] == "embed-model"

    def test_search_uses_manifest_metadata(self, tmp_path, monkeypatch):
        backend = QMDBackend(tmp_path)
        docs_dir = tmp_path / ".qmd-docs"
        docs_dir.mkdir(parents=True)
        encoded_filename = backend._filename_for_doc_id("amem:project/MEMORY")
        (docs_dir / encoded_filename).write_text("stored content")
        (docs_dir / "manifest.json").write_text(
            json.dumps(
                {
                    encoded_filename: {
                        "doc_id": "amem:project/MEMORY",
                        "source": "auto_memory",
                        "heading": "### project: Memory",
                        "date": None,
                        "metadata": {"file_path": "/tmp/project/MEMORY.md"},
                    }
                }
            )
        )

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == [
                "qmd",
                "--index",
                "observational-memory",
                "search",
                "memory",
                "-c",
                "observational-memory",
                "-n",
                "10",
                "--json",
            ]:
                return Result(
                    stdout=json.dumps(
                        [
                            {
                                "docid": "#mem001",
                                "file": f"qmd://observational-memory/{encoded_filename}",
                                "title": "Memory",
                                "snippet": "snippet",
                                "line": 7,
                            }
                        ]
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        results = backend.search("memory")

        assert len(results) == 1
        assert results[0].document.doc_id == "amem:project/MEMORY"
        assert results[0].document.source == DocumentSource.AUTO_MEMORY
        assert results[0].document.heading == "### project: Memory"
        assert results[0].document.content == "stored content"
        assert results[0].document.metadata["file_path"] == "/tmp/project/MEMORY.md"
        assert results[0].document.metadata["line"] == 7
        assert results[0].document.metadata["qmd_line"] == 7
        assert "source_line" not in results[0].document.metadata

    def test_search_uses_manifest_metadata_when_qmd_lowercases_legacy_filename(self, tmp_path, monkeypatch):
        backend = QMDBackend(tmp_path)
        docs_dir = tmp_path / ".qmd-docs"
        docs_dir.mkdir(parents=True)
        legacy_filename = "b2JzOjIwMjYtMDQtMDY.md"
        lowered_filename = legacy_filename.lower()
        (docs_dir / legacy_filename).write_text("stored content")
        (docs_dir / "manifest.json").write_text(
            json.dumps(
                {
                    legacy_filename: {
                        "doc_id": "obs:2026-04-06",
                        "source": "observations",
                        "heading": "## 2026-04-06",
                        "date": "2026-04-06",
                        "metadata": {
                            "file_path": "/tmp/observations.md",
                            "source_start_line": 5,
                        },
                    }
                }
            )
        )

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == [
                "qmd",
                "--index",
                "observational-memory",
                "search",
                "launchd",
                "-c",
                "observational-memory",
                "-n",
                "10",
                "--json",
            ]:
                return Result(
                    stdout=json.dumps(
                        [
                            {
                                "docid": "#obs001",
                                "file": f"qmd://observational-memory/{lowered_filename}",
                                "title": "2026-04-06",
                                "snippet": "launchd migration",
                                "line": 2,
                            }
                        ]
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        results = backend.search("launchd")

        assert len(results) == 1
        assert results[0].document.doc_id == "obs:2026-04-06"
        assert results[0].document.heading == "## 2026-04-06"
        assert results[0].document.metadata["file_path"] == "/tmp/observations.md"
        assert results[0].document.metadata["source_line"] == 6

    def test_search_maps_qmd_line_to_source_line(self, tmp_path, monkeypatch):
        backend = QMDBackend(tmp_path)
        docs_dir = tmp_path / ".qmd-docs"
        docs_dir.mkdir(parents=True)
        encoded_filename = backend._filename_for_doc_id("obs:2026-02-10")
        (docs_dir / encoded_filename).write_text("stored content")
        (docs_dir / "manifest.json").write_text(
            json.dumps(
                {
                    encoded_filename: {
                        "doc_id": "obs:2026-02-10",
                        "source": "observations",
                        "heading": "## 2026-02-10",
                        "date": "2026-02-10",
                        "metadata": {
                            "file_path": "/tmp/observations.md",
                            "source_start_line": 20,
                        },
                    }
                }
            )
        )

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == [
                "qmd",
                "--index",
                "observational-memory",
                "search",
                "launchd",
                "-c",
                "observational-memory",
                "-n",
                "10",
                "--json",
            ]:
                return Result(
                    stdout=json.dumps(
                        [
                            {
                                "docid": "#obs001",
                                "file": f"qmd://observational-memory/{encoded_filename}",
                                "title": "## 2026-02-10",
                                "snippet": "launchd migration",
                                "line": 12,
                            }
                        ]
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        results = backend.search("launchd")

        assert len(results) == 1
        assert results[0].document.metadata["file_path"] == "/tmp/observations.md"
        assert results[0].document.metadata["qmd_line"] == 12
        assert results[0].document.metadata["source_line"] == 31

    def test_search_returns_empty_when_qmd_missing(self, tmp_path, monkeypatch):
        def fake_run(args, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("subprocess.run", fake_run)

        backend = QMDBackend(tmp_path)
        assert backend.search("memory") == []

    def test_raw_search_output_uses_native_qmd_mode(self, tmp_path, monkeypatch):
        calls = []
        backend = QMDBackend(tmp_path, mode="query", index_name="om-review", no_rerank=True)

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            calls.append(args)
            if args == ["qmd", "--help"]:
                return Result(stdout="--index\n--no-rerank\nqmd bench")
            if args == [
                "qmd",
                "--index",
                "om-review",
                "query",
                "lex: launchd\nvec: launchd",
                "-c",
                "observational-memory",
                "-n",
                "5",
                "--no-rerank",
            ]:
                return Result(stdout="native qmd output\n")
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("shutil.which", lambda name: "/tmp/bin/qmd" if name == "qmd" else None)
        monkeypatch.setattr("subprocess.run", fake_run)

        stdout, stderr, returncode = backend.raw_search_output("launchd", limit=5)

        assert stdout == "native qmd output\n"
        assert stderr == ""
        assert returncode == 0
        assert any(call[4] == "lex: launchd\nvec: launchd" for call in calls if "query" in call)
        assert any(call[-1] == "--no-rerank" for call in calls if "query" in call)

    def test_legacy_fallback_doc_id_best_effort(self, tmp_path):
        backend = QMDBackend(tmp_path)
        assert backend._fallback_doc_id("obs_2026-02-10.md") == "obs:2026-02-10"
        assert backend._fallback_doc_id(backend._filename_for_doc_id("amem:project/MEMORY")) == "amem:project/MEMORY"

    def test_legacy_fallback_doc_id_ignores_non_utf8_hex_stems(self, tmp_path):
        backend = QMDBackend(tmp_path)
        assert backend._fallback_doc_id("80.md") == "80"


class TestQMDInspection:
    def test_qmd_collection_exists_parses_modern_list_output(self, monkeypatch):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == ["qmd", "--index", "om-review", "collection", "list"]:
                return Result(
                    stdout=(
                        "Collections (1):\n\n"
                        "observational-memory (qmd://observational-memory/)\n"
                        "  Pattern:  **/*.md\n"
                        "  Files:    4\n"
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        exists, raw_output, error = qmd_collection_exists("om-review", "observational-memory")

        assert exists is True
        assert error is None
        assert "observational-memory (qmd://observational-memory/)" in raw_output

    def test_inspect_qmd_index_parses_status_output(self, monkeypatch):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == ["qmd", "--index", "om-review", "collection", "list"]:
                return Result(stdout="observational-memory\tqmd://observational-memory/\n")
            if args == ["qmd", "--index", "om-review", "status"]:
                return Result(
                    stdout=(
                        "QMD Status\n\n"
                        "Index: /tmp/om-review.sqlite\n"
                        "Size:  3.5 MB\n\n"
                        "Documents\n"
                        "  Total:    21 files indexed\n"
                        "  Vectors:  10 embedded\n"
                        "  Pending:  11 need embedding (run 'qmd embed')\n"
                        "  Updated:  3m ago\n"
                    )
                )
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        info = inspect_qmd_index("om-review", "observational-memory")

        assert info == QMDIndexInfo(
            index_name="om-review",
            collection_name="observational-memory",
            collection_exists=True,
            index_path="/tmp/om-review.sqlite",
            total_files=21,
            vectors_embedded=10,
            pending_vectors=11,
            updated="3m ago",
            raw_output=(
                "QMD Status\n\n"
                "Index: /tmp/om-review.sqlite\n"
                "Size:  3.5 MB\n\n"
                "Documents\n"
                "  Total:    21 files indexed\n"
                "  Vectors:  10 embedded\n"
                "  Pending:  11 need embedding (run 'qmd embed')\n"
                "  Updated:  3m ago"
            ),
            error=None,
        )

    def test_inspect_qmd_index_does_not_match_collection_substrings(self, monkeypatch):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args == ["qmd", "--index", "om-review", "collection", "list"]:
                return Result(stdout="observational-memory-backup\tqmd://observational-memory-backup/\n")
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        info = inspect_qmd_index("om-review", "observational-memory")

        assert info.collection_exists is False
        assert info.error is None
        assert info.raw_output == "observational-memory-backup\tqmd://observational-memory-backup/"

    def test_is_ready_only_checks_collection_listing(self, tmp_path, monkeypatch):
        calls = []

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            calls.append(args)
            if args == ["qmd", "--index", "om-review", "collection", "list"]:
                return Result(stdout="observational-memory\tqmd://observational-memory/\n")
            raise AssertionError(f"Unexpected subprocess call: {args}")

        monkeypatch.setattr("subprocess.run", fake_run)

        backend = QMDBackend(tmp_path, index_name="om-review")
        assert backend.is_ready() is True
        assert calls == [["qmd", "--index", "om-review", "collection", "list"]]


# --- Reindex Tests ---


class TestReindex:
    def test_reindex_parses_and_indexes(self, tmp_path):
        empty_projects = tmp_path / "projects"
        empty_projects.mkdir()
        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=empty_projects)
        (tmp_path / "observations.md").write_text(SAMPLE_OBSERVATIONS)
        (tmp_path / "reflections.md").write_text(SAMPLE_REFLECTIONS)

        n = reindex(config)
        assert n == 5  # 2 observation dates + 3 reflection sections

        backend = get_backend("bm25", config)
        assert backend.is_ready()

    def test_reindex_empty_files(self, tmp_path):
        empty_projects = tmp_path / "projects"
        empty_projects.mkdir()
        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=empty_projects)
        (tmp_path / "observations.md").write_text("")
        (tmp_path / "reflections.md").write_text("")

        n = reindex(config)
        assert n == 0

    def test_reindex_missing_files(self, tmp_path):
        empty_projects = tmp_path / "projects"
        empty_projects.mkdir()
        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=empty_projects)
        n = reindex(config)
        assert n == 0
