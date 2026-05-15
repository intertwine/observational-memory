"""Tests for compact startup memory generation."""

from observational_memory.config import Config
from observational_memory.startup_memory import (
    build_startup_payload,
    ensure_startup_memory,
    recall_handle,
    refresh_startup_memory,
)

REFLECTIONS = """# Reflections — Long-Term Memory

*Last updated: 2026-03-11 04:21 UTC*
*Last reflected: 2026-03-11*

## Core Identity
- **Name:** Bryan Young
- **Role/occupation:** Software Engineer at Expel

## Active Projects
### Workbench MCP
- **Status:** Active
- Shipping production fixes

## Preferences & Opinions
- 🔴 Bottom-up migration
- 🔴 Parallel agent workflows
- 🟡 Small diffs

## Relationship & Communication
- Prefers concise summaries
- Corrects assumptions quickly

## Key Facts & Context
- 🔴 Primary repo: `~/experiments/workbench-mcp`
- 🔴 Uses git worktrees
- 🟡 Session timezone: US-based

## Recent Themes
- Production validation
- Code review velocity
"""


OBSERVATIONS = """# Observations

## 2026-03-10

### Current Context
- **Active task:** Older task

### Observations
- 🔴 Older observation

## 2026-03-11

### Current Context
- **Active task:** Compact startup memory work
- **Suggested next:** Validate the payload size

### Observations
- 🔴 Built the compact files
- 🟡 Misc detail
"""


def test_refresh_startup_memory_generates_compact_files(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(REFLECTIONS)
    config.observations_path.write_text(OBSERVATIONS)

    refresh_startup_memory(config)

    profile = config.profile_path.read_text()
    active = config.active_path.read_text()

    assert "# Startup Profile" in profile
    assert "## Core Identity" in profile
    assert "## Preferences & Opinions" in profile
    assert "## Relationship & Communication" in profile
    assert "Primary repo" in profile
    assert "Session timezone" not in profile

    assert "# Active Context" in active
    assert "## Active Projects" in active
    assert "## Recent Themes" in active
    assert "## Current Session Snapshot" in active
    assert "Compact startup memory work" in active
    assert "Older task" not in active


def test_ensure_startup_memory_refreshes_missing_files(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(REFLECTIONS)
    config.observations_path.write_text(OBSERVATIONS)

    ensure_startup_memory(config)

    assert config.profile_path.exists()
    assert config.active_path.exists()


def test_build_startup_payload_prioritizes_task_matching_context(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(
        REFLECTIONS + "\n## Creative & Professional\n" + "- " + ("album artwork detail " * 400) + "\n"
    )
    config.observations_path.write_text(OBSERVATIONS)

    payload = build_startup_payload(config, budget_chars=2200, cwd="/tmp/observational-memory", task="Workbench MCP")

    assert len(payload.text) <= 2200
    assert "Workbench MCP" in payload.text
    assert payload.overflow
    assert "om recall --handle" in payload.text or "om recall --query" in payload.text


def test_startup_payload_projects_large_profile_without_losing_recall(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    long_preferences = "\n".join(
        f"  - 🔴 Preference {index} for reviewable agent work <!--om: id=ome_{index:04d} kind=identity-->"
        for index in range(80)
    )
    config.reflections_path.write_text(
        "# Reflections — Long-Term Memory\n\n"
        "## Core Identity\n"
        "- **Name:** Bryan Young <!--om: id=ome_name kind=identity-->\n"
        "- **Role/occupation:** Software Engineer <!--om: id=ome_role kind=identity-->\n"
        "- **Communication style:** Direct and execution-focused <!--om: id=ome_style kind=identity-->\n"
        "- **Preferences:** <!--om: id=ome_preferences kind=identity-->\n"
        f"{long_preferences}\n\n"
        "## Active Projects\n\n"
        "### Observational Memory\n"
        "- **Status:** Active <!--om: id=ome_om kind=evergreen-->\n"
        "- Shaping startup payloads\n\n"
        "### Side Project\n"
        "- " + ("less relevant detail " * 120) + "\n"
    )
    config.observations_path.write_text(OBSERVATIONS)

    payload = build_startup_payload(
        config,
        budget_chars=6000,
        cwd="/tmp/observational-memory",
        task="startup payload shape",
        agent="codex",
    )

    assert len(payload.text) <= 6000
    assert "## Working Profile" in payload.text
    assert "Startup working contract" in payload.text
    assert "## Core Identity" not in payload.text
    assert "<!--om:" not in payload.text
    assert "## Active Projects / Observational Memory" in payload.text
    assert "om recall --handle startup:profile" in payload.text

    recalled_profile = recall_handle(config, "startup:profile")
    assert "## Core Identity" in recalled_profile
    assert "<!--om: id=ome_name" in recalled_profile
    assert "Preference 79" in recalled_profile


def test_recall_expands_projected_startup_subsection_handles(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(
        "# Reflections\n\n"
        "## Active Projects\n\n"
        "### Observational Memory\n"
        "- Startup payloads <!--om: id=ome_active kind=evergreen-->\n"
    )
    config.observations_path.write_text("# Observations\n")

    payload = build_startup_payload(config, budget_chars=4000, task="Observational Memory")

    assert "startup:active:active-projects:observational-memory" in payload.included_handles
    recalled = recall_handle(config, "startup:active:active-projects:observational-memory")
    assert "Startup payloads <!--om: id=ome_active" in recalled


def test_profile_identity_can_be_disabled_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_PROFILE_INCLUDE_IDENTITY", "0")
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(REFLECTIONS)
    config.observations_path.write_text(OBSERVATIONS)

    refresh_startup_memory(config)

    profile = config.profile_path.read_text()
    assert "## Core Identity" not in profile
    assert "## Preferences & Opinions" in profile
