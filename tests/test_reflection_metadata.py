from datetime import datetime, timezone

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.reflection_metadata import (
    ensure_reflection_metadata,
    filter_reflection_entries_for_cluster,
    filter_reflection_entries_for_host,
    find_reflection_conflicts,
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
    assert parse_metadata(lines[0])["source_type"] == "inferred"
    assert parse_metadata(lines[0])["actionability"] == "low"
    assert parse_metadata(lines[1])["id"] == "ome_keep"
    assert parse_metadata(lines[1])["custom"] == "yes"


def test_ensure_reflection_metadata_can_preserve_legacy_file_age():
    text = "# Reflections\n\n## Preferences & Opinions\n- Prefers concise handoffs\n"

    result = ensure_reflection_metadata(
        text,
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        source_mtime=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        node="node_a",
    )

    fields = parse_metadata(next(line for line in result.splitlines() if line.startswith("- ")))
    assert fields["last_seen"] == "2026-05-01T12:00:00Z"
    assert fields["kind"] == "preference"


def test_mode_kind_and_unknown_fields_round_trip():
    text = (
        "# Reflections\n\n"
        "## Working Mode\n"
        "- Execution mode: stop stopping <!--om: id=ome_keep custom=still-here-->\n"
    )

    result = ensure_reflection_metadata(text, now=datetime(2026, 5, 14, tzinfo=timezone.utc), node="node_a")

    fields = parse_metadata(next(line for line in result.splitlines() if line.startswith("- ")))
    assert fields["kind"] == "mode"
    assert fields["actionability"] == "high"
    assert fields["custom"] == "still-here"


def test_scope_local_entries_filter_from_cluster_and_remote_hosts():
    text = ensure_reflection_metadata(
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        "- Local only <!--om: scope=local node=node_a-->\n"
        "- Shared\n",
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        node="node_a",
    )

    assert "Local only" not in filter_reflection_entries_for_cluster(text)
    assert "Local only" in filter_reflection_entries_for_host(text, local_node="node_a")
    assert "Local only" not in filter_reflection_entries_for_host(text, local_node="node_b")
    assert "Shared" in filter_reflection_entries_for_cluster(text)


def test_find_reflection_conflicts_surfaces_non_snapshot_disagreements():
    a = ensure_reflection_metadata(
        "# Reflections\n\n## Preferences & Opinions\n- Prefers terse reports\n",
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        node="node_a",
    )
    b = ensure_reflection_metadata(
        "# Reflections\n\n## Preferences & Opinions\n- Prefers detailed reports\n",
        now=datetime(2026, 5, 14, tzinfo=timezone.utc),
        node="node_b",
    )

    conflicts = find_reflection_conflicts([("rec_a", "node_a", a), ("rec_b", "node_b", b)])

    assert len(conflicts) == 1
    assert conflicts[0].section == "Preferences & Opinions"
    assert conflicts[0].kind == "preference"


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
