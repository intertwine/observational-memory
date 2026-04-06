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


def test_search_text_output_shows_source_and_qmd_hit(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    monkeypatch.setattr("observational_memory.search.get_backend", lambda backend_name, config: _FakeBackend())

    result = runner.invoke(cli, ["search", "launchd"])

    assert result.exit_code == 0, result.output
    assert "Source: /tmp/observations.md:31" in result.output
    assert "QMD hit: qmd://observational-memory/obs_2026-02-10.md:12" in result.output
