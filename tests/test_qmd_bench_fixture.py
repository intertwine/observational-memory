"""Tests for the repo-local QMD benchmark fixture."""

import json
from pathlib import Path

ALLOWED_QUERY_TYPES = {"alias", "cross-domain", "exact", "semantic", "topical"}


def test_qmd_bench_fixture_references_existing_corpus_files():
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixture = json.loads((fixtures_dir / "qmd-bench-memory.json").read_text())
    corpus_dir = fixtures_dir / "qmd-bench-corpus"

    assert fixture["collection"] == "om-bench-memory"
    assert fixture["version"] == 1
    assert len(fixture["queries"]) == 6

    seen_ids: set[str] = set()
    for query in fixture["queries"]:
        assert query["id"] not in seen_ids
        seen_ids.add(query["id"])
        assert query["query"]
        assert query["type"] in ALLOWED_QUERY_TYPES
        assert query["expected_in_top_k"] >= 1
        assert query["expected_files"]
        for relative_path in query["expected_files"]:
            assert (corpus_dir / relative_path).is_file(), relative_path
