"""Tests for #50 startup context quality: dedup, freshness, cwd-scope, report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from observational_memory import startup_memory as sm
from observational_memory.config import Config


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def cfg(tmp_path):
    return Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")


def _write(cfg: Config, reflections: str, observations: str = "") -> None:
    cfg.ensure_memory_dir()
    cfg.reflections_path.write_text(reflections)
    if observations:
        cfg.observations_path.write_text(observations)
    sm.refresh_startup_memory(cfg)  # materialize profile.md / active.md


# --- dedup ---


def test_duplicate_profile_guidance_deduped_across_sections(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Core Identity\n- **Name:** Bryan Young\n\n"
        "## Preferences & Opinions\n- 🔴 Prefers concise summaries\n- 🔴 Uses uv for Python\n\n"
        "## Relationship & Communication\n- Prefers concise summaries\n"
    )
    _write(cfg, reflections)
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    # The bullet appears in two sections of reflections but only once in the payload.
    assert payload.text.lower().count("prefers concise summaries") == 1


def test_freshness_marker_does_not_defeat_dedup(cfg):
    # Same operational bullet in two sections with different last_seen: one copy
    # would be annotated stale, the other not. Dedup must still collapse them.
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- 🔴 tool version 1.2.3 installed <!--om: id=ome_1 kind=snapshot last_seen={_iso(40)}-->\n"
        "## Active Projects\n"
        f"- 🔴 tool version 1.2.3 installed <!--om: id=ome_2 kind=snapshot last_seen={_iso(1)}-->\n"
    )
    _write(cfg, reflections)
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    assert payload.text.lower().count("tool version 1.2.3 installed") == 1


def test_durable_survivor_not_marked_despite_duplicate_snapshot(cfg):
    # Same visible bullet: a durable preference (higher priority) and a snapshot
    # duplicate elsewhere, both old. The surviving durable copy must NOT be marked
    # stale even though the snapshot supplied a last_seen for the shared key.
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"  # higher priority, durable
        f"- 🔴 Prefers Python 3.11 for new projects <!--om: id=ome_1 kind=preference last_seen={_iso(90)}-->\n"
        "## Active Projects\n"  # lower priority, snapshot duplicate
        f"- 🔴 Prefers Python 3.11 for new projects <!--om: id=ome_2 kind=snapshot last_seen={_iso(90)}-->\n"
    )
    _write(cfg, reflections)
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    assert payload.text.lower().count("prefers python 3.11") == 1
    assert "verify" not in payload.text
    report = sm.startup_quality_report(cfg, budget_chars=24000)
    assert report["stale_operational_facts"] == []


def test_distinct_project_fields_are_not_deduped(cfg):
    # Sibling project subsections carry identical short fields that are DISTINCT
    # facts about distinct projects. Dedup must not collapse them or vanish a
    # whole project from both the payload and overflow.
    reflections = (
        "# Reflections\n\n"
        "## Active Projects\n"
        "### Alpha\n- Status: Active\n- Owner: Bryan\n"
        "### Beta\n- Status: Active\n- Owner: Bryan\n"
        "### Gamma\n- Status: Active\n- Owner: Bryan\n"
    )
    _write(cfg, reflections)
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    text = payload.text.lower()
    for project in ("alpha", "beta", "gamma"):
        assert project in text
    # Each project keeps its own Status/Owner fields (not collapsed to one).
    assert text.count("status: active") == 3
    assert text.count("owner: bryan") == 3


def test_fact_seen_recently_elsewhere_is_not_marked_stale(cfg):
    # Same fact: old copy in a higher-priority section, recent copy elsewhere.
    # The surviving (deduped) copy must NOT be marked stale — freshness uses the
    # freshest last_seen across all sections.
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"  # higher priority, OLD copy
        f"- 🔴 tool version 1.2.3 installed <!--om: id=ome_1 kind=snapshot last_seen={_iso(40)}-->\n"
        "## Active Projects\n"  # lower priority, RECENT copy
        f"- 🔴 tool version 1.2.3 installed <!--om: id=ome_2 kind=snapshot last_seen={_iso(1)}-->\n"
    )
    _write(cfg, reflections)
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    assert payload.text.lower().count("tool version 1.2.3 installed") == 1
    assert "verify" not in payload.text  # freshest sighting (1d) is within the window
    # Report agrees with the payload.
    report = sm.startup_quality_report(cfg, budget_chars=24000)
    assert report["stale_operational_facts"] == []


def test_dedupe_keeps_higher_priority_instance():
    high = sm.StartupChunk("profile", "Preferences & Opinions", "## Preferences\n- 🔴 Be direct", "h1", priority=10)
    low = sm.StartupChunk("active", "Recent Themes", "## Recent Themes\n- Be direct", "h2", priority=4)
    deduped, removed = sm._dedupe_startup_chunks([high, low])
    assert "be direct" in deduped[0].body.lower()
    assert "- be direct" not in deduped[1].body.lower()  # dropped from the lower-priority chunk
    assert removed == ["be direct"]


# --- freshness ---


def test_stale_operational_fact_is_annotated(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Active Projects\n"
        f"- 🔴 grok-cli running version 1.2.0 <!--om: id=ome_a kind=snapshot last_seen={_iso(40)}-->\n"
    )
    _write(cfg, reflections)
    # Freshness is applied at payload-build time (not baked into the file).
    payload = sm.build_startup_payload(cfg, budget_chars=24000)
    assert "as of" in payload.text and "verify" in payload.text
    assert "as of" not in cfg.active_path.read_text()  # materialized file stays raw


def test_recent_operational_fact_not_annotated(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Active Projects\n"
        f"- 🔴 grok-cli running version 1.2.0 <!--om: id=ome_a kind=snapshot last_seen={_iso(2)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in sm.build_startup_payload(cfg, budget_chars=24000).text


def test_non_operational_fact_not_annotated(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- 🔴 Prefers terse answers <!--om: id=ome_p kind=preference last_seen={_iso(60)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in sm.build_startup_payload(cfg, budget_chars=24000).text


def test_freshness_reflects_env_without_source_change(cfg, monkeypatch):
    # Changing OM_STARTUP_FRESHNESS_DAYS must affect the payload immediately,
    # without a source-file change — payload-time annotation guarantees this.
    reflections = (
        "# Reflections\n\n"
        "## Active Projects\n"
        f"- 🔴 grok-cli running version 1.2.0 <!--om: id=ome_a kind=snapshot last_seen={_iso(5)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in sm.build_startup_payload(cfg, budget_chars=24000).text  # 5d < default 14
    monkeypatch.setenv("OM_STARTUP_FRESHNESS_DAYS", "1")
    assert "as of" in sm.build_startup_payload(cfg, budget_chars=24000).text  # now 5d >= 1


def test_durable_kind_not_marked(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- 🔴 Prefers Python 3.11 for new projects <!--om: id=ome_p kind=preference last_seen={_iso(90)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in sm.build_startup_payload(cfg, budget_chars=24000).text


def test_route_match_normalizes_separators(cfg):
    # cwd slug "observational-memory" must boost a spaced heading "Observational Memory".
    p_obs = sm._chunk_priority(
        "active",
        "Active Projects / Observational Memory",
        "- work",
        cwd="/x/observational-memory",
        task=None,
        agent=None,
    )
    p_other = sm._chunk_priority(
        "active", "Active Projects / Code and Context", "- work", cwd="/x/observational-memory", task=None, agent=None
    )
    assert p_obs > p_other


# --- cwd / task scope ---


def test_current_task_project_wins_budget_over_unrelated_inventory(cfg):
    # Two sizeable active-project subsections; a tight budget can't fit both.
    alpha = "".join(f"- 🔴 alpha-service detail line number {i}\n" for i in range(70))
    zeta = "".join(f"- 🔴 zeta-tool detail line number {i}\n" for i in range(70))
    reflections = f"# Reflections\n\n## Active Projects\n### alpha-service\n{alpha}\n### zeta-tool\n{zeta}\n"
    _write(cfg, reflections)
    # Budget fits one ~2.8k-char project (matched, priority 14) plus header, but
    # not both — so the unrelated project overflows to a recall handle.
    payload = sm.build_startup_payload(
        cfg, budget_chars=3800, cwd="/home/bryan/code/alpha-service", task="alpha-service migration"
    )
    overflow_blob = " ".join(item["handle"] + item["heading"] for item in payload.overflow).lower()
    # The cwd-matched project's content gets first claim; the unrelated project's
    # content is NOT inlined — it overflows to a recall handle.
    assert "alpha-service detail line" in payload.text
    assert "zeta-tool detail line" not in payload.text
    assert "zeta-tool" in overflow_blob


# --- quality report ---


def test_quality_report_shape(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n- 🔴 Prefers concise summaries\n\n"
        "## Relationship & Communication\n- Prefers concise summaries\n\n"
        "## Active Projects\n"
        f"- 🔴 tool version 9.9.9 installed <!--om: id=ome_v kind=snapshot last_seen={_iso(99)}-->\n"
    )
    _write(cfg, reflections)
    report = sm.startup_quality_report(cfg, budget_chars=24000)
    assert report["duplicate_count"] >= 1
    assert "prefers concise summaries" in report["duplicate_bullets"]
    assert len(report["stale_operational_facts"]) >= 1
    assert report["stale_operational_facts"][0]["age_days"] >= 99
    assert report["budget_by_section"]  # at least one included section sized
    assert report["used_chars"] <= report["budget_chars"]
    # The reported stale text must not carry the baked-in marker (no double "as of").
    assert all("verify" not in fact["text"] for fact in report["stale_operational_facts"])


# --- review fixes ---


def test_dedup_does_not_orphan_nested_children():
    # A parent bullet with children is NOT deduped (would orphan the children);
    # a top-level leaf duplicate still is.
    high = sm.StartupChunk("profile", "A", "## A\n- Tooling\n  - uses pytest\n- Be direct", "h1", priority=10)
    low = sm.StartupChunk("active", "B", "## B\n- Tooling\n  - uses pytest\n- Be direct", "h2", priority=4)
    deduped, _removed = sm._dedupe_startup_chunks([high, low])
    low_body = deduped[1].body
    # The leaf "Be direct" is deduped away from the lower chunk...
    assert "- Be direct" not in low_body
    # ...but the parent "Tooling" (with a child) is preserved, child intact.
    assert "- Tooling" in low_body
    assert "uses pytest" in low_body


def test_quality_report_excludes_durable_kind_from_stale(cfg):
    # The report's stale list must agree with the payload: a durable-kind bullet
    # with version-like text is never reported as a stale operational fact.
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- 🔴 Prefers Python 3.11 for new projects <!--om: id=ome_p kind=preference last_seen={_iso(90)}-->\n"
        "## Active Projects\n"
        f"- 🔴 tool version 9.9.9 installed <!--om: id=ome_v kind=snapshot last_seen={_iso(90)}-->\n"
    )
    _write(cfg, reflections)
    report = sm.startup_quality_report(cfg, budget_chars=24000)
    texts = " ".join(fact["text"].lower() for fact in report["stale_operational_facts"])
    assert "python 3.11" not in texts  # durable preference excluded
    assert "tool version 9.9.9" in texts  # snapshot still reported


def test_route_terms_filter_generic_directory_and_filler():
    terms = sm._route_terms(cwd="/Users/me/code/alpha-service", task="the migration work", agent=None)
    assert "alpha-service" in terms
    assert "migration" in terms
    assert "code" not in terms  # generic container dir
    assert "work" not in terms  # generic filler
    assert "the" not in terms


def test_freshness_days_rejects_negative(monkeypatch):
    monkeypatch.setenv("OM_STARTUP_FRESHNESS_DAYS", "-5")
    assert sm._freshness_days() == sm.DEFAULT_STARTUP_FRESHNESS_DAYS
    monkeypatch.setenv("OM_STARTUP_FRESHNESS_DAYS", "nonsense")
    assert sm._freshness_days() == sm.DEFAULT_STARTUP_FRESHNESS_DAYS
    monkeypatch.setenv("OM_STARTUP_FRESHNESS_DAYS", "0")
    assert sm._freshness_days() == 0
