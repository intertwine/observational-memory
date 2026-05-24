"""Tests for the `om usage` CLI surface."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from observational_memory.cli import cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    for p in (home, config_dir, data_dir):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("OM_USAGE_TRACKING", "1")
    for key in ("OM_BUDGET_DAILY_USD", "OM_BUDGET_REFLECTOR_DAILY_USD", "OM_BUDGET_MODE"):
        monkeypatch.delenv(key, raising=False)
    return config_dir


def test_usage_status_empty(env):
    result = CliRunner().invoke(cli, ["usage", "status"])
    assert result.exit_code == 0, result.output
    assert "Usage (all time)" in result.output
    assert "calls: 0" in result.output


def test_usage_status_json_shape(env):
    result = CliRunner().invoke(cli, ["usage", "status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["tracking"] is True
    assert "summary" in data
    assert data["summary"]["calls"] == 0


def test_pricing_show_and_override_roundtrip(env):
    runner = CliRunner()
    show = runner.invoke(cli, ["usage", "pricing", "show"])
    assert show.exit_code == 0, show.output
    assert "Pricing snapshot:" in show.output

    set_res = runner.invoke(cli, ["usage", "pricing", "set", "--model", "demo-x", "--input", "1.0", "--output", "2.0"])
    assert set_res.exit_code == 0, set_res.output

    show2 = runner.invoke(cli, ["usage", "pricing", "show"])
    assert "demo-x" in show2.output
    assert "override" in show2.output

    reset = runner.invoke(cli, ["usage", "pricing", "reset"])
    assert reset.exit_code == 0
    show3 = runner.invoke(cli, ["usage", "pricing", "show"])
    assert "demo-x" not in show3.output


def _active_assignments(text: str) -> dict[str, str]:
    """Parse uncommented KEY=value lines (ignores template comment examples)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            out[key.strip()] = value.strip()
    return out


def test_budget_set_and_clear_writes_env(env):
    runner = CliRunner()
    env_path = env / "observational-memory" / "env"
    set_res = runner.invoke(
        cli, ["usage", "budget", "set", "--operation", "reflector", "--daily-usd", "1.00", "--soft"]
    )
    assert set_res.exit_code == 0, set_res.output
    active = _active_assignments(env_path.read_text())
    assert active.get("OM_BUDGET_REFLECTOR_DAILY_USD") == "1.00"
    assert active.get("OM_BUDGET_REFLECTOR_DAILY_USD_MODE") == "soft"

    clear = runner.invoke(cli, ["usage", "budget", "clear", "--operation", "reflector"])
    assert clear.exit_code == 0, clear.output
    active2 = _active_assignments(env_path.read_text())
    assert "OM_BUDGET_REFLECTOR_DAILY_USD" not in active2
    assert "OM_BUDGET_REFLECTOR_DAILY_USD_MODE" not in active2


def test_budget_set_requires_a_cap(env):
    result = CliRunner().invoke(cli, ["usage", "budget", "set"])
    assert result.exit_code != 0
    assert "at least one cap" in result.output


def test_doctor_reports_usage_subsystem(env):
    result = CliRunner().invoke(cli, ["doctor"])
    assert "Usage tracking" in result.output
