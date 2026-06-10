"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

# Non-OM_* ambient env that feeds Config()/provider resolution. A developer's
# real keys or regions must not leak into tests; tests that need these set them
# explicitly with monkeypatch.setenv.
_AMBIENT_PROVIDER_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "XAI_BASE_URL",
    "AWS_REGION",
    "CODEX_HOME",
    "GROK_HOME",
)


@pytest.fixture(autouse=True)
def _hermetic_om_env(monkeypatch):
    """Make every test hermetic against ambient OM configuration.

    ``Config()`` reads ``os.environ`` in dataclass default factories, so a
    developer shell with real OM settings (e.g. ``OM_LLM_MODEL=grok-4.3``,
    ``OM_BUDGET_MODE=soft``, ``OM_CLUSTER_ENABLED=1``) contaminates any test
    that constructs a ``Config``. Strip every ``OM_*`` variable plus provider
    credentials before each test. Tests that need specific values still work:
    this autouse fixture runs first, and test-level ``monkeypatch.setenv``
    calls land afterwards and override it.

    Also keep the usage subsystem from writing to a real DB during unrelated
    tests. Usage/budget tests opt back in by setting ``OM_USAGE_TRACKING=1``
    and pointing ``OM_USAGE_DB`` at a tmp path.
    """
    for key in list(os.environ):
        if key.startswith("OM_"):
            monkeypatch.delenv(key, raising=False)
    for key in _AMBIENT_PROVIDER_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OM_USAGE_TRACKING", "0")


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
