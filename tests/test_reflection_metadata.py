from datetime import datetime, timezone

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.reflection_metadata import (
    ensure_reflection_metadata,
    ensure_section_provenance,
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
        "# Reflections\n\n## Working Mode\n- Execution mode: stop stopping <!--om: id=ome_keep custom=still-here-->\n"
    )

    result = ensure_reflection_metadata(text, now=datetime(2026, 5, 14, tzinfo=timezone.utc), node="node_a")

    fields = parse_metadata(next(line for line in result.splitlines() if line.startswith("- ")))
    assert fields["kind"] == "mode"
    assert fields["actionability"] == "high"
    assert fields["custom"] == "still-here"


def test_scope_local_entries_filter_from_cluster_and_remote_hosts():
    text = ensure_reflection_metadata(
        "# Reflections\n\n## Preferences & Opinions\n- Local only <!--om: scope=local node=node_a-->\n- Shared\n",
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


# --- Gate 3: section-level rot-proof provenance ---------------------------

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_ensure_section_provenance_stamps_each_h2_when_window_given():
    doc = (
        "# Reflections\n\n"
        "## Core Identity\n- Name: Test\n\n"
        "## Active Projects\n- PR #33 is open\n\n"
        "## Preferences\n- Prefers terse replies\n"
    )
    out = ensure_section_provenance(doc, obs_window=("2026-05-28", "2026-05-31"), now=_NOW)
    marker = "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-28..2026-05-31-->"
    assert out.count("<!--om-section:") == 3
    for heading in ("## Core Identity", "## Active Projects", "## Preferences"):
        # The marker is the line immediately after the heading.
        lines = out.splitlines()
        idx = lines.index(heading)
        assert lines[idx + 1] == marker
    # Bullet/body content is otherwise untouched.
    assert "- Name: Test" in out
    assert "- PR #33 is open" in out
    assert "- Prefers terse replies" in out


def test_ensure_section_provenance_none_window_is_strict_noop():
    unstamped = "# Reflections\n\n## Active Projects\n- PR #33 is open\n"
    assert ensure_section_provenance(unstamped, obs_window=None, now=_NOW) == unstamped
    stamped = ensure_section_provenance(unstamped, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    # An already-stamped doc passes through byte-identical when window is None.
    assert ensure_section_provenance(stamped, obs_window=None, now=_NOW) == stamped


def test_ensure_section_provenance_is_idempotent():
    doc = "# Reflections\n\n## Active Projects\n- PR #33 is open\n"
    once = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    twice = ensure_section_provenance(once, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    assert once == twice
    assert twice.count("<!--om-section:") == 1


def test_section_provenance_does_not_touch_per_bullet_pass():
    doc = "# Reflections\n\n## Active Projects\n- PR #33 is open\n"
    per_bullet = ensure_reflection_metadata(doc, now=_NOW, node="local")
    stamped = ensure_section_provenance(per_bullet, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    # Per-bullet metadata is intact and unchanged.
    bullet_line = next(line for line in stamped.splitlines() if line.lstrip().startswith("- PR #33"))
    fields = parse_metadata(bullet_line)
    assert fields.get("scope") == "cluster"
    assert fields.get("node") == "local"
    assert "id" in fields and "kind" in fields
    # The section marker yields {} from parse_metadata (invisible to the bullet pass).
    marker_line = next(line for line in stamped.splitlines() if line.lstrip().startswith("<!--om-section:"))
    assert parse_metadata(marker_line) == {}
    # Re-running the per-bullet pass over the stamped doc does not duplicate or
    # mutate the section marker.
    rerun = ensure_reflection_metadata(stamped, now=_NOW, node="local")
    assert rerun.count("<!--om-section:") == 1


def test_section_stamp_only_on_h2_not_h3():
    doc = "# Reflections\n\n## Active Projects\n### Project Alpha\n- thing\n### Project Beta\n- other thing\n"
    out = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    lines = out.splitlines()
    # Exactly one marker, right after the H2; none after either H3.
    assert out.count("<!--om-section:") == 1
    assert lines[lines.index("## Active Projects") + 1].startswith("<!--om-section:")
    assert not lines[lines.index("### Project Alpha") + 1].startswith("<!--om-section:")
    assert not lines[lines.index("### Project Beta") + 1].startswith("<!--om-section:")


def test_section_provenance_degenerate_single_day_range():
    doc = "# Reflections\n\n## Active Projects\n- thing\n"
    out = ensure_section_provenance(doc, obs_window=("2026-05-31", "2026-05-31"), now=_NOW)
    assert "derived_from_obs_window=2026-05-31..2026-05-31" in out


def test_section_stamp_survives_cluster_filter_unchanged():
    doc = "# Reflections\n\n## Active Projects\n- PR #33 is open <!--om: scope=cluster-->\n"
    stamped = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    filtered = filter_reflection_entries_for_cluster(stamped)
    # A section with surviving shared content keeps its heading AND marker.
    assert "## Active Projects" in filtered
    assert filtered.count("<!--om-section:") == 1


def test_wholly_local_section_drops_heading_and_cadence_stamp_for_cluster():
    """Gate 3 leak guard: a section whose every bullet is scope=local must NOT
    leak its heading OR its `<!--om-section:` cadence/obs-window stamp into shared
    cluster memory, while a shared section keeps both."""
    doc = (
        "# Reflections\n\n"
        "## Secret Project\n"
        "- Pursuing Acme <!--om: scope=local node=laptop-->\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster node=laptop-->\n"
    )
    stamped = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    filtered = filter_reflection_entries_for_cluster(stamped)
    assert "Secret Project" not in filtered
    assert "## Shared" in filtered
    # Exactly one stamp survives — the shared section's; the local one is gone.
    assert filtered.count("<!--om-section:") == 1


def test_subsection_only_local_drops_subsection_title_and_stamp_for_cluster():
    """Gate 3 leak guard (PR #85 P1): a private H3/H4 subsection whose every
    bullet is scope=local must not leak its title — even when its parent H2 has
    other shared content. A heading is structure, never shared body, so an empty
    sub-block is pruned; a wholly-local H2 is dropped along with its stamp."""
    doc = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "### Public Initiative\n"
        "- Ship the docs <!--om: scope=cluster node=laptop-->\n"
        "### Secret Alpha Cadence\n"
        "- Weekly sync with Acme <!--om: scope=local node=laptop-->\n\n"
        "## Secret Standalone\n"
        "### Hidden Detail\n"
        "- Private note <!--om: scope=local node=laptop-->\n"
    )
    stamped = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    filtered = filter_reflection_entries_for_cluster(stamped)
    # The shared subsection survives; both private subsection titles do not.
    assert "Public Initiative" in filtered
    assert "Secret Alpha Cadence" not in filtered
    assert "Hidden Detail" not in filtered
    # The wholly-local H2 (only an empty private subsection) is dropped entirely,
    # taking its cadence stamp with it; the shared H2 keeps exactly its one stamp.
    assert "Secret Standalone" not in filtered
    assert "## Active Projects" in filtered
    assert filtered.count("<!--om-section:") == 1
