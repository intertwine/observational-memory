"""Shared fixtures for auth tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    """Point the auth store at a temp directory and clear inherited env.

    Tests in this folder exercise ``om login`` flows that intentionally write
    to ``os.environ`` directly (so an in-process ``Config()`` reload sees the
    new provider/model). ``monkeypatch.delenv`` only tracks env vars that were
    set *before* it was called; direct ``os.environ[k] = v`` writes from the
    code under test on previously-unset keys leak past test teardown
    (reproducible in pytest 9.x). To prevent ordering-dependent failures in
    later tests (e.g. ``tests/usage/test_llm_usage.py``), snapshot the OM/LLM
    auth-related env at fixture entry and restore it on teardown.
    """
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("OM_AUTH_FILE", str(auth_file))
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for var in (
        "CODEX_HOME",
        "GROK_HOME",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "OM_LLM_PROVIDER",
        "SSH_CLIENT",
        "SSH_TTY",
    ):
        monkeypatch.delenv(var, raising=False)

    def _is_tracked(key: str) -> bool:
        return key.startswith(("OM_LLM_", "OM_AUTH_", "ANTHROPIC_", "OPENAI_", "XAI_"))

    snapshot = {k: v for k, v in os.environ.items() if _is_tracked(k)}
    yield auth_file
    leaked = {k for k in os.environ if _is_tracked(k)} - set(snapshot)
    for k in leaked:
        del os.environ[k]
    for k, v in snapshot.items():
        if os.environ.get(k) != v:
            os.environ[k] = v
