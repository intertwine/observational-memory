from datetime import datetime, timezone

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.reflection_metadata import (
    ensure_reflection_metadata,
    parse_metadata,
    prune_stale_snapshots,
)


def test_ensure_reflection_metadata_adds_and_preserves_fields():
    text = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "- PR #33 is open\n"
        "- Existing <!--om: id=ome_keep kind=evergreen last_seen=2026-05-01T00:00:00Z custom=yes-->\n"
    )

    result = ensure_reflection_metadata(text, now=datetime(2026, 5, 14, tzinfo=timezone.utc), node="node_a")

    lines = [line for line in result.splitlines() if line.startswith("- ")]
    assert parse_metadata(lines[0])["kind"] == "snapshot"
    assert parse_metadata(lines[0])["node"] == "node_a"
    assert parse_metadata(lines[1])["id"] == "ome_keep"
    assert parse_metadata(lines[1])["custom"] == "yes"


def test_prune_stale_snapshots_moves_entries_idempotently():
    text = ensure_reflection_metadata(
        "# Reflections\n\n## Active Projects\n- PR #33 is open\n- Durable convention\n",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
        node="node_a",
    )

    pruned, summary = prune_stale_snapshots(
        text,
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        ttl_days=14,
        action="stale-section",
    )
    pruned_again, summary_again = prune_stale_snapshots(
        pruned,
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        ttl_days=14,
        action="stale-section",
    )

    assert summary.stale_sectioned == 1
    assert "## Stale snapshots" in pruned
    assert "Durable convention" in pruned
    assert pruned_again == pruned
    assert summary_again.stale_sectioned == 0


def test_prune_command_writes_reflections(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config = Config()
    config.ensure_memory_dir()
    config.reflections_path.write_text(
        ensure_reflection_metadata(
            "# Reflections\n\n## Active Projects\n- PR #33 is open\n",
            now=datetime(2026, 4, 1, tzinfo=timezone.utc),
            node="local",
        )
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["prune", "--json"])

    assert result.exit_code == 0, result.output
    assert "stale_sectioned" in result.output
