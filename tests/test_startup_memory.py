"""Tests for compact startup memory generation."""

from observational_memory.config import Config
from observational_memory.startup_memory import (
    ensure_startup_memory,
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
