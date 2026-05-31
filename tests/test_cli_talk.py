"""Tests for the `om talk` CLI command."""

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
    def __init__(self, ready=True):
        self._ready = ready

    def is_ready(self):
        return self._ready

    def search(self, query, limit=10):
        return [
            SearchResult(
                document=Document(
                    doc_id="ref:active-projects",
                    source=DocumentSource.REFLECTIONS,
                    heading="## Active Projects",
                    content="## Active Projects\nBuilding the talk-to-memories feature.",
                ),
                score=0.9,
                rank=1,
            )
        ]

    def index(self, documents):
        pass


def _install_fakes(monkeypatch, *, ready=True, compress=None):
    monkeypatch.setattr(
        "observational_memory.search.get_backend",
        lambda backend_name, config: _FakeBackend(ready=ready),
    )
    if compress is None:

        def compress(system, user, config, **kw):
            return "I remember you're building the talk-to-memories feature."

    monkeypatch.setattr("observational_memory.llm.compress", compress)


def test_talk_grounded_conversation(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_fakes(monkeypatch, ready=True)
    runner = CliRunner()

    result = runner.invoke(cli, ["talk"], input="what am I working on?\nexit\n")
    assert result.exit_code == 0, result.output
    assert "om> I remember you're building" in result.output


def test_talk_json_transcript(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_fakes(monkeypatch, ready=True)
    runner = CliRunner()

    result = runner.invoke(cli, ["talk", "--json", "--query", "hello"], input="")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["backend_ready"] is True
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["user"] == "hello"
    assert payload["turns"][0]["grounded"] is True
    assert payload["turns"][0]["recalled"][0]["doc_id"] == "ref:active-projects"


def test_talk_max_turns(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_fakes(monkeypatch, ready=True)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["talk", "--json", "--max-turns", "2"],
        input="one\ntwo\nthree\nfour\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["turns"]) == 2


def test_talk_ungrounded_when_backend_unavailable(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _install_fakes(monkeypatch, ready=False)
    runner = CliRunner()

    result = runner.invoke(cli, ["talk", "--json", "--query", "hello"], input="")
    assert result.exit_code == 0, result.output
    assert "unavailable" in result.output
    payload = json.loads(result.stdout)
    assert payload["turns"][0]["grounded"] is False


def test_talk_backend_override_passed_through(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    seen = {}

    def fake_get_backend(backend_name, config):
        seen["backend"] = backend_name
        return _FakeBackend(ready=True)

    monkeypatch.setattr("observational_memory.search.get_backend", fake_get_backend)
    monkeypatch.setattr("observational_memory.llm.compress", lambda s, u, c, **k: "ok")
    runner = CliRunner()

    result = runner.invoke(cli, ["talk", "--backend", "moss", "--query", "hi"], input="")
    assert result.exit_code == 0, result.output
    assert seen["backend"] == "moss"
