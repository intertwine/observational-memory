import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.reflection_metadata import (
    SHAREABLE_SCOPES,
    _scope_is_shareable,
    diff_reflection_conflicts,
    ensure_reflection_metadata,
    ensure_section_provenance,
    filter_reflection_document_for_shareout,
    filter_reflection_entries_for_cluster,
    filter_reflection_entries_for_host,
    find_reflection_conflicts,
    parse_metadata,
    prune_stale_snapshots,
)

# Table-driven share-out matrix (Gate 4): the block-level privacy rule, exercised
# across every Markdown continuation shape that previously leaked one-by-one. Each
# case names a private token that MUST NOT survive share-out and/or a shared token
# that MUST. `S` = must be withheld; `K` = must ride along.
_S = "ACMESECRETTOKEN"
_K = "KEEPSHARED"
_SHAREOUT_MATRIX = [
    ("tight indented continuation", f"## P\n- secret <!--om: scope=local-->\n  {_S}\n", [_S], []),
    ("lazy same-indent continuation", f"## P\n- secret <!--om: scope=local-->\n{_S}\n", [_S], []),
    ("nested unscoped child bullet", f"## P\n- secret <!--om: scope=team-->\n  - {_S}\n", [_S], []),
    ("deep nested unscoped child", f"## P\n- secret <!--om: scope=local-->\n  - a\n    - {_S}\n", [_S], []),
    (
        "loose prose after blank stays inside item",
        f"## P\n- secret <!--om: scope=local-->\n\n  {_S} loose paragraph\n\n## Q\n- ok <!--om: scope=cluster-->\n",
        [_S],
        ["ok"],
    ),
    (
        "loose child bullet after blank stays inside item",
        f"## P\n- secret <!--om: scope=local-->\n\n  - {_S} loose child\n\n## Q\n- ok <!--om: scope=cluster-->\n",
        [_S],
        ["ok"],
    ),
    ("prose line carrying its own scope", f"## P\nprose {_S} <!--om: scope=local-->\n", [_S], []),
    ("withheld entry at EOF with lazy tail", f"## P\n- secret <!--om: scope=org-->\ntail {_S}\n", [_S], []),
    (
        "explicitly scoped shareable child survives a withheld parent",
        f"## P\n- secret <!--om: scope=local-->\n  - {_K} <!--om: scope=cluster-->\n",
        [],
        [_K],
    ),
    (
        "explicitly scoped withheld child dropped under a shareable parent",
        f"## P\n- {_K} <!--om: scope=cluster-->\n  - {_S} <!--om: scope=local-->\n",
        [_S],
        [_K],
    ),
    (
        "blank releases UNINDENTED prose as independent/shared",
        f"## P\n- secret <!--om: scope=local-->\n\n{_K} independent prose\n",
        [],
        [_K],
    ),
    (
        "sibling shareable bullet after withheld bullet is kept",
        f"## P\n- secret <!--om: scope=local-->\n- {_K} <!--om: scope=cluster-->\n",
        [],
        [_K],
    ),
    (
        "shared bullet keeps its own tight continuation",
        f"## P\n- {_K} bullet <!--om: scope=cluster-->\n  more {_K} detail\n",
        [],
        [_K],
    ),
    (
        "heading boundary ends the withheld entry",
        f"## P\n- secret {_S} <!--om: scope=local-->\n## Q\n- {_K} <!--om: scope=cluster-->\n",
        [_S],
        [_K],
    ),
]


@pytest.mark.parametrize("name, doc, absent, present", _SHAREOUT_MATRIX, ids=[c[0] for c in _SHAREOUT_MATRIX])
def test_shareout_block_matrix(name, doc, absent, present):
    """Gate 4 block-level share-out matrix: no withheld token leaks, no shared token
    is over-withheld, across every Markdown continuation/child/boundary shape."""
    out = filter_reflection_document_for_shareout(doc)
    for token in absent:
        assert token not in out, f"{name}: withheld token leaked"
    for token in present:
        assert token in out, f"{name}: shared token over-withheld"


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


def test_diff_no_conflict_when_identical():
    doc = "## Core Identity\n- Name: Bryan <!--om: kind=identity id=ome_a actionability=high -->\n"
    assert diff_reflection_conflicts(doc, doc) == []


def test_diff_singleton_slot_divergence_without_id_echo():
    prior = "## Core Identity\n- Name: Bryan <!--om: kind=identity actionability=high -->\n"
    new = "## Core Identity\n- Name: Bryan Young <!--om: kind=identity actionability=high -->\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1
    assert conflicts[0].section == "Core Identity"
    assert conflicts[0].kind == "identity"
    sides = {e["side"]: e["text"] for e in conflicts[0].entries}
    assert sides == {"prior": "Name: Bryan", "new": "Name: Bryan Young"}
    assert {e["signal"] for e in conflicts[0].entries} == {"slot"}


def test_diff_id_divergence_when_metadata_comment_echoed():
    prior = "## Policy\n- Never force push <!--om: kind=policy id=ome_xyz actionability=high -->\n"
    new = "## Policy\n- Always force push <!--om: kind=policy id=ome_xyz actionability=high -->\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1
    assert {e["signal"] for e in conflicts[0].entries} == {"id"}


def test_diff_no_false_positive_when_section_gains_entry():
    prior = "## Prefs\n- Prefers tabs <!--om: kind=preference id=ome_1 -->\n"
    new = (
        "## Prefs\n- Prefers tabs <!--om: kind=preference id=ome_1 -->\n"
        "- Prefers dark mode <!--om: kind=preference id=ome_2 -->\n"
    )
    assert diff_reflection_conflicts(prior, new) == []


def test_diff_no_false_positive_on_unchanged_multi_fact_section():
    # The cross-host heuristic would flag this slot (two differing texts, two
    # records); the prior-vs-new diff must not, because nothing changed.
    doc = (
        "## Prefs\n- Prefers tabs <!--om: kind=preference id=ome_1 -->\n"
        "- Prefers dark mode <!--om: kind=preference id=ome_2 -->\n"
    )
    assert diff_reflection_conflicts(doc, doc) == []


def test_diff_ignores_local_scope_and_snapshot_kinds():
    prior = (
        "## Core Identity\n- Name: Bryan <!--om: kind=identity scope=local actionability=high -->\n"
        "## Status\n- Working on PR #1 <!--om: kind=snapshot actionability=low -->\n"
    )
    new = (
        "## Core Identity\n- Name: Changed <!--om: kind=identity scope=local actionability=high -->\n"
        "## Status\n- Working on PR #2 <!--om: kind=snapshot actionability=low -->\n"
    )
    assert diff_reflection_conflicts(prior, new) == []


def test_diff_id_signal_reported_once_not_double_counted_by_slot():
    # Same explicit id AND a singleton slot — must surface a single conflict.
    prior = "## Mode\n- Mode: cautious <!--om: kind=mode id=ome_m actionability=high -->\n"
    new = "## Mode\n- Mode: aggressive <!--om: kind=mode id=ome_m actionability=high -->\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1
    assert {e["signal"] for e in conflicts[0].entries} == {"id"}


def test_diff_empty_prior_is_noop():
    new = "## Core Identity\n- Name: Bryan <!--om: kind=identity actionability=high -->\n"
    assert diff_reflection_conflicts("", new) == []


@pytest.mark.parametrize(
    "prior_text, new_text",
    [
        ("Name: Bryan Young", "Name: **Bryan Young**"),  # markdown bold
        ("Name: Bryan Young", "Name: `Bryan Young`"),  # backticks
        ("Name: Bryan", "Name:  Bryan"),  # internal whitespace
        ("Never force-push without approval", "Never force-push without approval."),  # trailing period
        ('Prefers "concise" replies', "Prefers “concise” replies"),  # smart quotes
        ("Range is 2026-01 - 2026-02", "Range is 2026-01 – 2026-02"),  # en-dash
    ],
)
def test_diff_normalization_suppresses_cosmetic_changes(prior_text, new_text):
    prior = f"## Core Identity\n- {prior_text} <!--om: kind=identity actionability=high -->\n"
    new = f"## Core Identity\n- {new_text} <!--om: kind=identity actionability=high -->\n"
    assert diff_reflection_conflicts(prior, new) == [], "cosmetic restyle must not be a conflict"


def test_diff_downgrade_catches_guardrail_loosened_to_evergreen():
    # The new side drops the policy trigger word AND would re-infer as evergreen;
    # anchoring on the prior side's high-stakes classification still surfaces it.
    prior = "## Deployment\n- Production deploys must not run without a second approver.\n"
    new = "## Deployment\n- Production deploys can proceed with a single approver.\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1
    assert {e["signal"] for e in conflicts[0].entries} == {"downgrade"}


def test_diff_downgrade_catches_explicit_kind_and_scope_downgrade():
    prior = "## Policy\n- Never deploy on Friday. <!--om: kind=policy -->\n"
    new = "## Policy\n- Deploy any day including Friday. <!--om: kind=policy scope=local -->\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1


def test_diff_downgrade_requires_solo_section_both_sides():
    # New side gains a sibling bullet -> not solo -> ambiguous (add vs edit) -> skip.
    prior = "## Deployment\n- Production deploys must not run without approval.\n"
    new = "## Deployment\n- Production deploys can proceed.\n- Also enabled canary rollouts.\n"
    assert diff_reflection_conflicts(prior, new) == []


def test_diff_does_not_double_report_section_across_signals():
    # Kind-preserved singleton + solo section: exactly one conflict, no duplicate.
    prior = "## Mode\n- Working mode: cautious <!--om: kind=mode actionability=high -->\n"
    new = "## Mode\n- Working mode: aggressive <!--om: kind=mode actionability=high -->\n"
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 1


def test_diff_reports_independent_singleton_kinds_in_one_section():
    # Codex P2: two singleton high-stakes facts of DIFFERENT kinds under one
    # heading, both changed, must BOTH report (section-level dedup hid one).
    prior = (
        "## Core Identity\n"
        "- Name: Bryan <!--om: kind=identity actionability=high -->\n"
        "- Prefers concise replies <!--om: kind=preference actionability=medium -->\n"
    )
    new = (
        "## Core Identity\n"
        "- Name: Bryan Young <!--om: kind=identity actionability=high -->\n"
        "- Prefers expansive replies <!--om: kind=preference actionability=medium -->\n"
    )
    conflicts = diff_reflection_conflicts(prior, new)
    assert len(conflicts) == 2
    assert {c.kind for c in conflicts} == {"identity", "preference"}


def _setup_conflict_cli(tmp_path, monkeypatch, new_doc):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config = Config()
    config.ensure_memory_dir()
    prior = "# Reflections\n\n## Core Identity\n- Name: Bryan <!--om: kind=identity actionability=high -->\n"
    config.reflections_path.write_text(prior)
    monkeypatch.setattr("observational_memory.reflect.run_reflector", lambda cfg, dry: new_doc)
    return config


def test_reflect_check_conflicts_reports_singleton_divergence(tmp_path, monkeypatch):
    new_doc = "# Reflections\n\n## Core Identity\n- Name: Bryan Young <!--om: kind=identity actionability=high -->\n"
    _setup_conflict_cli(tmp_path, monkeypatch, new_doc)
    result = CliRunner().invoke(cli, ["reflect", "--check-conflicts"])
    assert result.exit_code == 0, result.stderr
    assert "1 high-stakes reflection conflict" in result.stderr
    assert "Name: Bryan Young" in result.stderr


def test_reflect_check_conflicts_json_is_pure_stdout(tmp_path, monkeypatch):
    new_doc = "# Reflections\n\n## Core Identity\n- Name: Bryan Young <!--om: kind=identity actionability=high -->\n"
    _setup_conflict_cli(tmp_path, monkeypatch, new_doc)
    result = CliRunner().invoke(cli, ["reflect", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["conflicts"]) == 1
    assert payload["conflicts"][0]["kind"] == "identity"
    assert "Running reflector" not in result.stdout  # chatter must stay off stdout


def test_reflect_check_conflicts_clean_when_unchanged(tmp_path, monkeypatch):
    new_doc = "# Reflections\n\n## Core Identity\n- Name: Bryan <!--om: kind=identity actionability=high -->\n"
    _setup_conflict_cli(tmp_path, monkeypatch, new_doc)
    result = CliRunner().invoke(cli, ["reflect", "--check-conflicts"])
    assert result.exit_code == 0, result.stderr
    assert "No high-stakes reflection conflicts" in result.stderr


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


# --- Gate 4: pluggable share-out allowlist (default-deny, fail-closed) -------


def test_scope_resolver_default_deny_unit():
    """The pure resolver: absent rides along, only allowlist members share, every
    explicit non-member (typo/hallucinated/future/empty) fails closed."""
    assert _scope_is_shareable(None) is True
    assert _scope_is_shareable("cluster") is True
    assert _scope_is_shareable("local") is False
    assert _scope_is_shareable("team") is False
    assert _scope_is_shareable("org") is False
    assert _scope_is_shareable("locol") is False
    assert _scope_is_shareable("") is False


def test_shareable_scopes_allowlist_ships_cluster_only():
    """Mechanism-only tripwire: no inert team/org values may be added."""
    assert SHAREABLE_SCOPES == frozenset({"cluster"})
    assert "team" not in SHAREABLE_SCOPES
    assert "org" not in SHAREABLE_SCOPES


def test_explicit_unknown_scopes_withheld_from_cluster():
    """Behavior delta: an explicit non-cluster scope no longer leaks; a sibling
    scope=cluster bullet in the same section still shares."""
    doc = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "- Typo bullet <!--om: scope=locol-->\n"
        "- Hallucinated bullet <!--om: scope=team-->\n"
        "- Future bullet <!--om: scope=org-->\n"
        "- Shared <!--om: scope=cluster-->\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "Typo bullet" not in filtered
    assert "Hallucinated bullet" not in filtered
    assert "Future bullet" not in filtered
    assert "Shared" in filtered


def test_absent_scope_structure_rides_along_for_cluster():
    """Unstamped hand-typed bullets and all absent-scope structure (preamble,
    *Last reflected:*, blanks, H2/H3 headings, <!--om-section:--> stamp) survive."""
    doc = (
        "# Reflections\n\n"
        "*Last reflected: 2026-06-01*\n\n"
        "## Active Projects\n"
        "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-30..2026-05-31-->\n"
        "### Subsection\n"
        "- Hand-typed unstamped bullet\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "Hand-typed unstamped bullet" in filtered
    assert "# Reflections" in filtered
    assert "*Last reflected: 2026-06-01*" in filtered
    assert "## Active Projects" in filtered
    assert "### Subsection" in filtered
    assert "<!--om-section:" in filtered


def test_explicit_unknown_only_section_is_pruned_for_cluster():
    """Gate-3 composition holds for the new withhold: a section whose only bullet
    is an explicit-unknown scope drops its heading and <!--om-section:--> stamp,
    exactly as a wholly-local section does."""
    doc = (
        "# Reflections\n\n"
        "## Team Secret\n"
        "- Org roadmap <!--om: scope=team-->\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster-->\n"
    )
    stamped = ensure_section_provenance(doc, obs_window=("2026-05-30", "2026-05-31"), now=_NOW)
    filtered = filter_reflection_entries_for_cluster(stamped)
    assert "Team Secret" not in filtered
    assert "Org roadmap" not in filtered
    assert "## Shared" in filtered
    assert filtered.count("<!--om-section:") == 1


def test_withheld_bullet_continuation_line_does_not_leak_for_cluster():
    """PR #86 re-review P1: a withheld bullet's indented continuation line carries
    no <!--om: ...--> metadata, so a per-line filter let it ride along as
    absent-scope content and leak. The share-out filter must drop the continuation
    along with its bullet — and the now-empty section heading with it."""
    doc = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "- Team-only plan <!--om: scope=team node=laptop-->\n"
        "  continuation naming Acme private cadence\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster node=laptop-->\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "continuation naming Acme private cadence" not in filtered
    assert "Team-only plan" not in filtered
    assert "Active Projects" not in filtered  # section emptied -> heading pruned
    assert "Public fact" in filtered  # a shared bullet elsewhere is untouched


def test_withheld_bullet_lazy_continuation_does_not_leak_for_cluster():
    """PR #86 re-review P1 (lazy continuation): a same-indent absent-scope line
    directly after a withheld bullet, with no blank/heading/list boundary, is a
    CommonMark lazy continuation of that list item — it must be withheld too, not
    kept as independent prose."""
    doc = (
        "# Reflections\n\n"
        "## Private\n"
        "- Secret plan <!--om: scope=local node=laptop-->\n"
        "lazy continuation naming Acme private cadence\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster node=laptop-->\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "lazy continuation naming Acme private cadence" not in filtered
    assert "Secret plan" not in filtered
    assert "Private" not in filtered  # section emptied -> heading pruned
    assert "Public fact" in filtered


def test_withheld_bullet_nested_unscoped_child_does_not_leak_for_cluster():
    """PR #86 re-review P1 (nested child): a nested UNSCOPED child list item under
    a withheld parent is part of the parent item in Markdown, so it must be
    withheld too — not treated as a sibling boundary and emitted."""
    doc = (
        "# Reflections\n\n"
        "## Private\n"
        "- Secret plan <!--om: scope=local node=laptop-->\n"
        "  - nested Acme private cadence\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster node=laptop-->\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "nested Acme private cadence" not in filtered
    assert "Secret plan" not in filtered
    assert "Private" not in filtered
    assert "Public fact" in filtered


def test_explicitly_scoped_nested_child_is_judged_on_its_own_scope():
    """A nested child that carries its OWN scope is judged on that scope, never
    inheriting the withheld parent's: a scope=cluster child under a scope=local
    parent is shared; a scope=local child under a scope=cluster parent is withheld."""
    doc = (
        "# Reflections\n\n"
        "## A\n"
        "- Parent secret <!--om: scope=local-->\n"
        "  - Child public KEEPME <!--om: scope=cluster-->\n"
        "## B\n"
        "- Parent public <!--om: scope=cluster-->\n"
        "  - Child secret DROPME <!--om: scope=local-->\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "KEEPME" in filtered
    assert "Parent secret" not in filtered
    assert "DROPME" not in filtered
    assert "Parent public" in filtered


def test_blank_line_releases_independent_prose_for_cluster():
    """PR #86 re-review P2: a blank line is a hard block boundary. Absent-scope
    prose AFTER a blank is an independent block and rides along (shared) — the
    withheld entry must not swallow it."""
    doc = (
        "# Reflections\n\n"
        "## Notes\n"
        "- Secret plan <!--om: scope=local-->\n\n"
        "Independent shared prose that follows a blank line.\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "Secret plan" not in filtered
    assert "Independent shared prose that follows a blank line." in filtered


def test_shared_bullet_continuation_line_is_preserved_for_cluster():
    """Parity guard: a SHARED bullet's continuation must still ride along, so the
    continuation-aware filter only withholds continuations of WITHHELD bullets."""
    doc = (
        "# Reflections\n\n"
        "## Shared\n"
        "- Public plan <!--om: scope=cluster node=laptop-->\n"
        "  continuation with more public detail\n"
    )
    filtered = filter_reflection_entries_for_cluster(doc)
    assert "Public plan" in filtered
    assert "continuation with more public detail" in filtered


def test_realistic_corpus_byte_identical_to_pre_gate4():
    """Default-preserving: for a real corpus of only {cluster, local, absent},
    the generalized filter is byte-for-byte the OLD `!= local` behavior."""
    raw = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "- Ship voice feature\n"
        "- Private spike <!--om: scope=local node=laptop-->\n"
        "## Preferences & Opinions\n"
        "- Prefers concise handoffs\n"
    )
    stamped = ensure_section_provenance(
        ensure_reflection_metadata(raw, now=_NOW, node="laptop"),
        obs_window=("2026-05-30", "2026-05-31"),
        now=_NOW,
    )
    # Reconstruct the pre-Gate-4 behavior inline: keep iff scope != "local",
    # then the unchanged Gate-3 pruning + reassembly.
    from observational_memory.reflection_metadata import _drop_empty_heading_sections

    old_kept = [line for line in stamped.splitlines() if parse_metadata(line).get("scope") != "local"]
    old_expected = "\n".join(_drop_empty_heading_sections(old_kept)).rstrip() + "\n"
    assert filter_reflection_entries_for_cluster(stamped) == old_expected


def test_self_heal_asymmetry_explicit_unknown_stays_withheld_absent_heals():
    """Guards the documented Gate-4 safety claim (reflection_metadata.py docstring +
    docs/om-cluster-sync.md): `ensure_reflection_metadata` uses setdefault, so an
    explicit-unknown scope (typo) is NOT rewritten and stays WITHHELD across reflects,
    while an absent-scope bullet self-heals to scope=cluster and resumes SHARING. If a
    future change flips setdefault->unconditional assignment, the typo would be silently
    re-stamped scope=cluster and leak off-host — this test must fail loudly first."""
    doc = "# Reflections\n\n## Active Projects\n- Typo bullet <!--om: scope=locol-->\n- Hand bullet\n"
    out = ensure_reflection_metadata(doc, now=_NOW, node="laptop")
    lines = out.splitlines()
    typo_line = next(line for line in lines if line.lstrip().startswith("- Typo bullet"))
    hand_line = next(line for line in lines if line.lstrip().startswith("- Hand bullet"))
    # (a) setdefault did NOT overwrite the explicit typo; it stays withheld.
    assert parse_metadata(typo_line).get("scope") == "locol"
    assert "Typo bullet" not in filter_reflection_entries_for_cluster(out)
    # (b) the unstamped bullet self-healed to scope=cluster and now shares.
    assert parse_metadata(hand_line).get("scope") == "cluster"
    assert "Hand bullet" in filter_reflection_entries_for_cluster(out)
