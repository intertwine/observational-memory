"""Tests for the pluggable search module."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from observational_memory.config import Config
from observational_memory.search import (
    Document,
    DocumentSource,
    SearchResult,
    get_backend,
    reindex,
)
from observational_memory.search.parser import parse_observations, parse_reflections
from observational_memory.search.bm25 import BM25Backend, _tokenize
from observational_memory.search.none import NoneBackend


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
        from observational_memory.search.qmd import QMDBackend
        config = Config(memory_dir=tmp_path)
        backend = get_backend("qmd", config)
        assert isinstance(backend, QMDBackend)


# --- Reindex Tests ---


class TestReindex:
    def test_reindex_parses_and_indexes(self, tmp_path):
        config = Config(memory_dir=tmp_path, search_backend="bm25")
        (tmp_path / "observations.md").write_text(SAMPLE_OBSERVATIONS)
        (tmp_path / "reflections.md").write_text(SAMPLE_REFLECTIONS)

        n = reindex(config)
        assert n == 5  # 2 observation dates + 3 reflection sections

        backend = get_backend("bm25", config)
        assert backend.is_ready()

    def test_reindex_empty_files(self, tmp_path):
        config = Config(memory_dir=tmp_path, search_backend="bm25")
        (tmp_path / "observations.md").write_text("")
        (tmp_path / "reflections.md").write_text("")

        n = reindex(config)
        assert n == 0

    def test_reindex_missing_files(self, tmp_path):
        config = Config(memory_dir=tmp_path, search_backend="bm25")
        n = reindex(config)
        assert n == 0
