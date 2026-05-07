"""Tests for platform memory export bundles."""

from datetime import datetime, timezone

import pytest

from observational_memory.config import Config
from observational_memory.platform_export import export_platform_memory

REFLECTIONS = """# Reflections - Long-Term Memory

*Last updated: 2026-05-07 12:00 UTC*

## Core Identity
- **Name:** Bryan
- **Preferences:** Direct, concrete answers

## Active Projects
### Observational Memory
- **Status:** Active
- Preparing platform memory compatibility
"""

OBSERVATIONS = """# Observations

## 2026-05-07

### Current Context
- **Active task:** Export platform memory
"""


def _config_with_memory(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(REFLECTIONS)
    config.observations_path.write_text(OBSERVATIONS)
    return config


def test_chatgpt_export_uses_compact_memory_seed(tmp_path):
    config = _config_with_memory(tmp_path)
    output_dir = tmp_path / "chatgpt-export"

    result = export_platform_memory(config, target="chatgpt", output_dir=output_dir)

    assert result.output_dir == output_dir
    seed = output_dir / "chatgpt-memory-seed.md"
    assert seed.exists()
    text = seed.read_text()
    assert "## Stable profile" in text
    assert "## Active context" in text
    # export_platform_memory refreshes derived startup files from reflections
    # before building the platform seed.
    assert "Direct, concrete answers" in text
    assert "Recent observations" not in text
    assert (output_dir / "manifest.json").exists()


def test_claude_managed_agents_export_splits_focused_memory_files(tmp_path):
    config = _config_with_memory(tmp_path)
    output_dir = tmp_path / "claude-export"

    export_platform_memory(config, target="claude-managed-agents", output_dir=output_dir)

    assert (output_dir / "memories" / "profile.md").exists()
    assert (output_dir / "memories" / "active-context.md").exists()
    assert (output_dir / "memories" / "reflections" / "core-identity.md").exists()
    assert (output_dir / "memories" / "reflections" / "active-projects.md").exists()
    assert not (output_dir / "memories" / "recent-observations.md").exists()


def test_export_can_include_observations_and_uses_default_output_dir(tmp_path):
    config = _config_with_memory(tmp_path)
    generated_at = datetime(2026, 5, 7, 12, 34, tzinfo=timezone.utc)

    result = export_platform_memory(
        config,
        target="generic",
        include_observations=True,
        generated_at=generated_at,
    )

    assert result.output_dir == config.memory_dir / "exports" / "generic-20260507T123400Z"
    assert (result.output_dir / "observations.md").exists()


def test_export_refuses_non_empty_output_without_overwrite(tmp_path):
    config = _config_with_memory(tmp_path)
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    (output_dir / "old.txt").write_text("old")

    with pytest.raises(FileExistsError):
        export_platform_memory(config, target="generic", output_dir=output_dir)

    export_platform_memory(config, target="generic", output_dir=output_dir, overwrite=True)

    assert not (output_dir / "old.txt").exists()
    assert (output_dir / "manifest.json").exists()


def test_export_refuses_memory_root_output(tmp_path):
    config = _config_with_memory(tmp_path)

    with pytest.raises(ValueError):
        export_platform_memory(config, target="generic", output_dir=config.memory_dir)


def test_export_accepts_claude_alias_for_programmatic_callers(tmp_path):
    config = _config_with_memory(tmp_path)
    output_dir = tmp_path / "claude-export"

    result = export_platform_memory(config, target="claude", output_dir=output_dir)

    assert result.target == "claude-managed-agents"
    assert (output_dir / "memories" / "profile.md").exists()


def test_export_rejects_unknown_target(tmp_path):
    config = _config_with_memory(tmp_path)

    with pytest.raises(ValueError, match="Unknown export target"):
        export_platform_memory(config, target="not-a-platform", output_dir=tmp_path / "export")


def test_export_refuses_output_ancestor_of_memory_dir(tmp_path):
    config = _config_with_memory(tmp_path)

    with pytest.raises(ValueError):
        export_platform_memory(config, target="generic", output_dir=tmp_path)


def test_claude_export_chunks_large_reflection_with_continuation_headings(tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    config.ensure_memory_dir()
    config.reflections_path.write_text(
        "# Reflections\n\n"
        "## Very Large Section\n" + "\n".join(f"- Item {index}: {'x' * 1000}" for index in range(120)) + "\n"
    )
    config.observations_path.write_text("")
    output_dir = tmp_path / "large-claude-export"

    export_platform_memory(config, target="claude-managed-agents", output_dir=output_dir)

    chunked = sorted((output_dir / "memories" / "reflections").glob("very-large-section-part-*.md"))
    assert len(chunked) > 1
    assert chunked[0].read_text().startswith("## Very Large Section")
    assert chunked[1].read_text().startswith("## Very Large Section (continued, part 2)")
    assert chunked[1].stat().st_size <= 95_000
