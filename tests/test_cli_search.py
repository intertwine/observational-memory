"""Tests for search CLI output formatting."""

import json

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.search import Document, DocumentSource, SearchResult


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    for path in (home, xdg_config, xdg_data, codex_home):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _fake_results() -> list[SearchResult]:
    return [
        SearchResult(
            document=Document(
                doc_id="obs:2026-02-10",
                source=DocumentSource.OBSERVATIONS,
                heading="## 2026-02-10",
                content="## 2026-02-10\n\nlaunchd migration\nmore context",
                metadata={
                    "file_path": "/tmp/observations.md",
                    "source_line": 31,
                    "qmd_file": "qmd://observational-memory/obs_2026-02-10.md",
                    "qmd_docid": "#abc123",
                    "qmd_line": 12,
                },
            ),
            score=0.91,
            rank=1,
        )
    ]


class _FakeBackend:
    def is_ready(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        assert query == "launchd"
        assert limit == 10
        return _fake_results()

    def raw_search_output(self, query: str, limit: int = 10) -> tuple[str, str, int]:
        assert query == "launchd"
        assert limit == 10
        return "\x1b]8;;qmd://observational-memory/hit\x1b\\\\launchd hit\x1b]8;;\x1b\\\\\n", "", 0


def test_search_json_includes_source_and_qmd_metadata(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _FakeBackend())

    result = runner.invoke(cli, ["search", "launchd", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["source_path"] == "/tmp/observations.md"
    assert payload[0]["source_line"] == 31
    assert payload[0]["qmd_file"] == "qmd://observational-memory/obs_2026-02-10.md"
    assert payload[0]["qmd_docid"] == "#abc123"
    assert payload[0]["qmd_line"] == 12
    assert "source_start_line" not in payload[0]["metadata"]


class _StampedBackend:
    """Backend returning a stamped reflection section (raw provenance comments)."""

    def is_ready(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return [
            SearchResult(
                document=Document(
                    doc_id="ref:core-identity",
                    source=DocumentSource.REFLECTIONS,
                    heading="## Core Identity",
                    content=(
                        "## Core Identity\n"
                        "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-28..2026-05-31-->\n"
                        "- Name: Alex <!--om: id=ome_x kind=identity scope=cluster-->"
                    ),
                    metadata={"file_path": "/tmp/reflections.md", "source_line": 3},
                ),
                score=0.88,
                rank=1,
            )
        ]


def test_search_text_output_strips_provenance_comments(monkeypatch, tmp_path):
    """PR #85 P3: the human terminal snippet must use the stripped payload
    content, never r.document.content, so it never prints a raw `<!--om-section:`
    stamp or per-bullet `<!--om:` metadata (the --json path already strips it)."""
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _StampedBackend())

    result = runner.invoke(cli, ["search", "Alex"])

    assert result.exit_code == 0, result.output
    assert "<!--om-section:" not in result.output
    assert "<!--om:" not in result.output
    # The real fact still shows through.
    assert "Name: Alex" in result.output


def test_search_text_output_shows_source_and_qmd_hit(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _FakeBackend())

    result = runner.invoke(cli, ["search", "launchd"])

    assert result.exit_code == 0, result.output
    assert "Source: /tmp/observations.md:31" in result.output
    assert "QMD hit: qmd://observational-memory/obs_2026-02-10.md:12" in result.output


def test_search_raw_qmd_passthrough(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _FakeBackend())

    result = runner.invoke(cli, ["search", "launchd", "--raw-qmd"])

    assert result.exit_code == 0, result.output
    assert "launchd hit" in result.output


def test_search_raw_qmd_rejects_json(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["search", "launchd", "--raw-qmd", "--json"])

    assert result.exit_code != 0
    assert "--raw-qmd cannot be combined with --json" in result.output


def test_search_raw_qmd_requires_qmd_backend(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    class _NoRawBackend:
        def is_ready(self) -> bool:
            return True

        def search(self, query: str, limit: int = 10) -> list[SearchResult]:
            return []

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _NoRawBackend())

    result = runner.invoke(cli, ["search", "launchd", "--raw-qmd"])

    assert result.exit_code != 0
    assert "--raw-qmd is only available with qmd and qmd-hybrid backends" in result.output


def test_search_raw_qmd_suppresses_reindex_banner(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _FakeBackend())
    monkeypatch.setattr("observational_memory.search.reindex", lambda config: 7)

    result = runner.invoke(cli, ["search", "launchd", "--raw-qmd", "--reindex"])

    assert result.exit_code == 0, result.output
    assert "launchd hit" in result.output
    assert "Indexed 7 document(s)" not in result.output
