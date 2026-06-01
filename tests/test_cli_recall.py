"""Tests for the `om recall` command's recall-status surface."""

import json

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.search import Document, DocumentSource, SearchResult


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    for path in (home, xdg_config, xdg_data):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))


class _FakeBackend:
    def __init__(self, ready=True, results=None, search_raises=False):
        self._ready = ready
        self._results = results or []
        self._search_raises = search_raises

    def is_ready(self):
        return self._ready

    def search(self, query, limit=10):
        if self._search_raises:
            raise RuntimeError("degenerate corpus / subprocess died")
        return self._results[:limit]

    def index(self, documents):
        pass


def _hit():
    return SearchResult(
        document=Document(
            doc_id="ref:active-projects",
            source=DocumentSource.REFLECTIONS,
            heading="## Active Projects",
            content="Building the talk-to-memories feature.",
        ),
        score=0.9,
        rank=1,
    )


def _install_backend(monkeypatch, *, ready=True, results=None, search_raises=False):
    monkeypatch.setattr(
        "observational_memory.search.get_backend",
        lambda backend_name, config: _FakeBackend(ready=ready, results=results, search_raises=search_raises),
    )


def test_recall_json_status_ok(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=True, results=[_hit()])
    result = CliRunner().invoke(cli, ["recall", "--query", "projects", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recall_status"] == "ok"
    assert payload["results"][0]["doc_id"] == "ref:active-projects"


def test_recall_json_status_empty(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=True, results=[])
    result = CliRunner().invoke(cli, ["recall", "--query", "projects", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recall_status"] == "empty"
    assert payload["results"] == []


def test_recall_json_status_unavailable(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=False, results=[_hit()])
    result = CliRunner().invoke(cli, ["recall", "--query", "projects", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recall_status"] == "unavailable"
    assert payload["results"] == []


def test_recall_human_unavailable_message(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=False, results=[_hit()])
    result = CliRunner().invoke(cli, ["recall", "--query", "projects"])
    assert result.exit_code == 0, result.output
    assert "is unavailable" in result.output
    # The remediation must point at a real command (om reindex does not exist).
    assert "om search --reindex" in result.output
    assert "om reindex" not in result.output.replace("om search --reindex", "")
    assert "No recall results." not in result.output


def test_recall_human_empty_message(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=True, results=[])
    result = CliRunner().invoke(cli, ["recall", "--query", "projects"])
    assert result.exit_code == 0, result.output
    assert "No recall results." in result.output


def test_recall_ready_backend_search_raises_is_unavailable(monkeypatch, tmp_path):
    # is_ready() passes but search() raises (degenerate corpus, QMD subprocess
    # error). Must degrade to recall_status="unavailable", not traceback.
    _set_base_env(monkeypatch, tmp_path)
    _install_backend(monkeypatch, ready=True, search_raises=True)
    result = CliRunner().invoke(cli, ["recall", "--query", "projects", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recall_status"] == "unavailable"
    assert payload["results"] == []


def test_recall_handle_only_json_has_recall_status(monkeypatch, tmp_path):
    # A handle-only --json payload must still carry recall_status for a uniform
    # contract (consumers read payload["recall_status"] without a KeyError).
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "observational_memory.startup_memory.recall_handle",
        lambda config, handle: "expanded handle text",
    )
    result = CliRunner().invoke(cli, ["recall", "--handle", "startup:active:x", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recall_status"] == "ok"
    assert payload["text"] == "expanded handle text"
