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
    active = cfg.active_path.read_text()
    assert "as of" in active and "verify" in active


def test_recent_operational_fact_not_annotated(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Active Projects\n"
        f"- 🔴 grok-cli running version 1.2.0 <!--om: id=ome_a kind=snapshot last_seen={_iso(2)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in cfg.active_path.read_text()


def test_non_operational_fact_not_annotated(cfg):
    reflections = (
        "# Reflections\n\n"
        "## Preferences & Opinions\n"
        f"- 🔴 Prefers terse answers <!--om: id=ome_p kind=preference last_seen={_iso(60)}-->\n"
    )
    _write(cfg, reflections)
    assert "as of" not in cfg.profile_path.read_text()


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
