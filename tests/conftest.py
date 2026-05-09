"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_om_home(tmp_path, monkeypatch):
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
    monkeypatch.delenv("OM_CLUSTER_ENABLED", raising=False)
    monkeypatch.delenv("OM_CLUSTER_ID", raising=False)
    return tmp_path
