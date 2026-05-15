"""Tests for compact startup context injection."""

import json

from click.testing import CliRunner

from observational_memory.cli import cli


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    for p in (home, xdg_config, xdg_data, codex_home):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_context_prefers_compact_startup_files(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(
        "# Reflections\n\n## Core Identity\n- Name: Bryan\n\n## Active Projects\n- Project: MCP\n"
    )
    (memory_dir / "observations.md").write_text(
        "# Observations\n\n## 2026-03-11\n\n### Current Context\n- **Active task:** Context compaction\n"
    )

    result = runner.invoke(cli, ["context"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "# Observational Memory Startup Context" in ctx
    assert "## Startup Routing" in ctx
    assert "## Core Identity" in ctx
    assert "## Active Projects" in ctx
    assert "## Recall" in ctx
    assert "## Long-Term Memory (Reflections)" not in ctx
    assert "## Recent Observations" not in ctx


def test_context_budget_emits_overflow_handles(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(
        "# Reflections\n\n"
        "## Core Identity\n- Name: Bryan\n\n"
        "## Active Projects\n- " + ("large project detail " * 500) + "\n"
    )
    (memory_dir / "observations.md").write_text("# Observations\n")

    result = runner.invoke(cli, ["context", "--budget-chars", "2000", "--task", "startup budget"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 2000
    assert "## Startup Overflow" in ctx
    assert "`startup:active:active-projects`" in ctx


def test_recall_expands_startup_handle(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text("# Reflections\n\n## Active Projects\n- Project: OM Cluster\n")
    (memory_dir / "observations.md").write_text("# Observations\n")

    result = runner.invoke(cli, ["recall", "--handle", "startup:active:active-projects"])

    assert result.exit_code == 0, result.output
    assert "Project: OM Cluster" in result.output
