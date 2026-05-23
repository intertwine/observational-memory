"""Shared fixtures for auth tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    """Point the auth store at a temp directory and clear inherited env."""
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
    return auth_file
