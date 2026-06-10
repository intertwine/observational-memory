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


def test_quality_report_json_includes_growth_block(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(
        "# Reflections\n\n"
        "## Core Identity\n"
        "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-20..2026-05-30-->\n"
        "- Name: Bryan\n\n"
        "## Active Projects\n### Alpha\n- Status: Active\n"
    )

    result = runner.invoke(cli, ["context", "--quality-report", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    growth = report["growth"]
    assert [d["name"] for d in growth["documents"]] == [
        "reflections.md",
        "profile.md",
        "active.md",
        "observations.md",
    ]
    headings = [s["heading"] for s in growth["sections"]]
    assert headings == ["Core Identity", "Active Projects"]
    core = growth["sections"][0]
    assert core["last_activity"] == "2026-06-01"
    assert isinstance(core["age_days"], int)
    # Active Projects has no recoverable timestamp: coldness unknown, never a guess.
    assert growth["sections"][1]["age_days"] is None
    assert growth["totals"]["total_bytes"] > 0


def test_quality_report_text_includes_growth_section(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text("# Reflections\n\n## Active Projects\n- Project: OM\n")

    result = runner.invoke(cli, ["context", "--quality-report"])

    assert result.exit_code == 0, result.output
    assert "memory growth (B0):" in result.output
    assert "total memory:" in result.output
    assert "reflections.md:" in result.output


def test_quality_report_with_no_memory_files_exits_zero(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["context", "--quality-report", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    growth = report["growth"]
    assert growth["sections"] == []
    # The quality-report path itself materializes profile.md/active.md (existing
    # behavior), but reflections.md stays absent and is measured as empty.
    reflections = next(d for d in growth["documents"] if d["name"] == "reflections.md")
    assert reflections["exists"] is False
    assert reflections["bytes"] == 0
    assert "error" not in growth
