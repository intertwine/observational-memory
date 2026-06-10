"""Tests for the v0.8.0 Gate 6 B0 growth measurement (pure, read-only)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from observational_memory.config import Config
from observational_memory.growth import (
    format_bytes,
    format_growth_lines,
    growth_doctor_checks,
    measure_memory_growth,
)

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")


FIXTURE_REFLECTIONS = (
    "# Reflections\n"
    "\n"
    "*Last reflected: 2026-06-01*\n"
    "\n"
    "## Core Identity\n"
    "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-20..2026-05-30-->\n"
    "- **Name:** Bryan Young <!--om: id=ome_1 kind=identity last_seen=2026-05-15T10:00:00Z scope=cluster-->\n"
    "\n"
    "## Active Projects\n"
    "\n"
    "### Alpha\n"
    "- Status: Active <!--om: id=ome_2 kind=snapshot last_seen=2026-06-08T09:00:00Z-->\n"
    "- Owner: Bryan\n"
    "\n"
    "### Beta\n"
    "- Status: Paused <!--om: id=ome_3 kind=snapshot last_seen=2026-01-02T09:00:00Z-->\n"
    "\n"
    "## Old Notes\n"
    "- prose bullet with no timestamps at all\n"
    "- another untimestamped bullet\n"
)


def _write(cfg: Config, reflections: str | None = None, observations: str | None = None) -> None:
    cfg.ensure_memory_dir()
    if reflections is not None:
        cfg.reflections_path.write_text(reflections)
    if observations is not None:
        cfg.observations_path.write_text(observations)


def _section(report: dict, heading: str) -> dict:
    return next(s for s in report["sections"] if s["heading"] == heading)


# --- per-section / per-subsection sizes ---


def test_per_section_sizes_computed_from_fixture(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    report = measure_memory_growth(cfg, now=NOW)

    assert [s["heading"] for s in report["sections"]] == ["Core Identity", "Active Projects", "Old Notes"]

    doc_bytes = len(FIXTURE_REFLECTIONS.encode("utf-8"))
    reflections_doc = next(d for d in report["documents"] if d["name"] == "reflections.md")
    assert reflections_doc["exists"] is True
    assert reflections_doc["bytes"] == doc_bytes
    assert report["totals"]["reflections_bytes"] == doc_bytes

    core = _section(report, "Core Identity")
    core_text = FIXTURE_REFLECTIONS.split("## Core Identity")[1].split("## Active Projects")[0]
    core_text = "## Core Identity" + core_text.rstrip("\n")
    assert core["bytes"] == len(core_text.encode("utf-8"))
    assert core["lines"] == len(core_text.splitlines())
    assert core["bullets"] == 1
    assert core["share"] == pytest.approx(core["bytes"] / doc_bytes, abs=1e-3)

    projects = _section(report, "Active Projects")
    assert projects["bullets"] == 3
    assert [sub["heading"] for sub in projects["subsections"]] == ["Alpha", "Beta"]
    alpha = projects["subsections"][0]
    assert alpha["bullets"] == 2
    assert alpha["lines"] == 3  # heading + two bullets
    assert 0 < alpha["share"] < projects["share"]

    assert report["totals"]["section_count"] == 3
    assert report["totals"]["subsection_count"] == 2


def test_document_totals_cover_all_four_documents(cfg):
    _write(cfg, FIXTURE_REFLECTIONS, observations="# Observations\n\n## 2026-06-09\n- saw a thing\n")
    # refresh_startup_memory not run: profile.md/active.md missing is fine.
    report = measure_memory_growth(cfg, now=NOW)
    names = [d["name"] for d in report["documents"]]
    assert names == ["reflections.md", "profile.md", "active.md", "observations.md"]
    obs = next(d for d in report["documents"] if d["name"] == "observations.md")
    assert obs["exists"] is True and obs["bullets"] == 1
    assert report["totals"]["total_bytes"] == sum(d["bytes"] for d in report["documents"])


# --- coldness ---


def test_coldness_from_inline_metadata_timestamps(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    report = measure_memory_growth(cfg, now=NOW)

    # Core Identity: section stamp last_reflected=2026-06-01 beats bullet last_seen 2026-05-15.
    core = _section(report, "Core Identity")
    assert core["last_activity"] == "2026-06-01"
    assert core["age_days"] == 9

    # Active Projects: freshest bullet (Alpha, 2026-06-08) wins for the H2.
    projects = _section(report, "Active Projects")
    assert projects["last_activity"] == "2026-06-08"
    assert projects["age_days"] == 2
    # Per-subsection coldness derives from each subsection's own content.
    alpha, beta = projects["subsections"]
    assert alpha["last_activity"] == "2026-06-08"
    assert beta["last_activity"] == "2026-01-02"
    assert beta["age_days"] == (NOW - datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)).days


def test_unknown_coldness_is_null_never_a_guess(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    report = measure_memory_growth(cfg, now=NOW)
    old = _section(report, "Old Notes")
    assert old["last_activity"] is None
    assert old["age_days"] is None
    assert report["unknown_coldness_sections"] == ["Old Notes"]


def test_coldness_from_dated_heading(cfg):
    _write(cfg, "# Reflections\n\n## Recent Themes\n\n### 2026-06-05\n- a theme\n")
    report = measure_memory_growth(cfg, now=NOW)
    section = _section(report, "Recent Themes")
    assert section["last_activity"] == "2026-06-05"
    assert section["age_days"] == 5


def test_coldness_from_obs_window_end(cfg):
    _write(
        cfg,
        "# Reflections\n\n## Stamped\n<!--om-section: derived_from_obs_window=2026-05-01..2026-06-03-->\n- a bullet\n",
    )
    report = measure_memory_growth(cfg, now=NOW)
    assert _section(report, "Stamped")["last_activity"] == "2026-06-03"


def test_future_timestamp_clamps_age_to_zero(cfg):
    future = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(cfg, f"# Reflections\n\n## Skewed\n- fact <!--om: last_seen={future}-->\n")
    report = measure_memory_growth(cfg, now=NOW)
    assert _section(report, "Skewed")["age_days"] == 0


def test_timezone_naive_now_is_handled(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    report = measure_memory_growth(cfg, now=datetime(2026, 6, 10, 12, 0, 0))
    assert _section(report, "Core Identity")["age_days"] == 9


# --- top-N rankings ---


def test_fattest_and_coldest_rankings(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    report = measure_memory_growth(cfg, now=NOW, top_n=2)
    assert len(report["fattest_sections"]) == 2
    assert report["fattest_sections"][0]["heading"] == "Active Projects"
    assert report["fattest_sections"][0]["bytes"] >= report["fattest_sections"][1]["bytes"]
    # Coldest: only sections with a KNOWN age qualify; oldest first.
    assert [s["heading"] for s in report["coldest_sections"]] == ["Core Identity", "Active Projects"]


# --- failure modes ---


def test_missing_files_yield_empty_valid_report(cfg):
    report = measure_memory_growth(cfg, now=NOW)
    assert report["sections"] == []
    assert report["fattest_sections"] == []
    assert report["coldest_sections"] == []
    assert report["totals"]["total_bytes"] == 0
    assert all(d["exists"] is False for d in report["documents"])
    assert "error" not in report


def test_malformed_markdown_does_not_raise(cfg):
    _write(
        cfg,
        "#### lonely deep heading\n## \n###\n- \n* x\n##no-space\nplain prose\n"
        "<!--om: last_seen=not-a-date-->\n<!--om-section: last_reflected=9999-99-99-->\n",
    )
    report = measure_memory_growth(cfg, now=NOW)
    assert "error" not in report
    # Malformed timestamps are skipped, never guessed.
    for section in report["sections"]:
        assert section["age_days"] is None or isinstance(section["age_days"], int)


def test_non_utf8_bytes_measured_best_effort(cfg):
    cfg.ensure_memory_dir()
    raw = b"\xff\xfe## Section\n- bullet \x80\x81\n"
    cfg.reflections_path.write_bytes(raw)
    report = measure_memory_growth(cfg, now=NOW)
    assert "error" not in report
    doc = next(d for d in report["documents"] if d["name"] == "reflections.md")
    assert doc["bytes"] == len(raw)  # byte sizes stay accurate from the raw file


def test_whitespace_only_document(cfg):
    _write(cfg, "   \n\n\t\n")
    report = measure_memory_growth(cfg, now=NOW)
    assert report["sections"] == []
    assert "error" not in report


def test_duplicate_section_names_stay_distinct(cfg):
    _write(cfg, "# Reflections\n\n## Twin\n- first copy\n\n## Twin\n- second copy\n- third\n")
    report = measure_memory_growth(cfg, now=NOW)
    twins = [s for s in report["sections"] if s["heading"] == "Twin"]
    assert len(twins) == 2
    assert twins[0]["bullets"] == 1
    assert twins[1]["bullets"] == 2


def test_measurement_is_read_only(cfg):
    _write(cfg, FIXTURE_REFLECTIONS, observations="# Observations\n\n## 2026-06-09\n- saw a thing\n")
    paths = [cfg.reflections_path, cfg.observations_path]
    before = [(os.stat(p).st_mtime_ns, p.read_bytes()) for p in paths]
    listing_before = sorted(p.name for p in cfg.memory_dir.iterdir())

    measure_memory_growth(cfg, now=NOW)

    after = [(os.stat(p).st_mtime_ns, p.read_bytes()) for p in paths]
    assert before == after  # mtimes and contents unchanged
    assert sorted(p.name for p in cfg.memory_dir.iterdir()) == listing_before  # nothing created


def test_internal_error_never_escapes(cfg, monkeypatch):
    monkeypatch.setattr(
        "observational_memory.growth._measure",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = measure_memory_growth(cfg, now=NOW)
    assert report["error"] == "boom"
    assert report["sections"] == []  # still a valid, empty report shape


# --- rendering helpers ---


def test_format_bytes():
    assert format_bytes(412) == "412 B"
    assert format_bytes(38093) == "37.2 KB"
    assert format_bytes(1_500_000) == "1.4 MB"


def test_growth_doctor_checks_compact_block(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    checks = growth_doctor_checks(measure_memory_growth(cfg, now=NOW))
    names = [name for name, _status, _detail in checks]
    assert names == ["Memory growth (B0)", "Memory growth: largest section", "Memory growth: coldest section"]
    assert all(status == "PASS" for _name, status, _detail in checks)
    summary = checks[0][2]
    assert "reflections.md" in summary and "3 section(s)" in summary
    assert "Active Projects" in checks[1][2]
    assert "coldness unknown for 1 section(s)" in checks[2][2]


def test_growth_doctor_checks_missing_memory(cfg):
    checks = growth_doctor_checks(measure_memory_growth(cfg, now=NOW))
    assert checks[0][1] == "PASS"
    assert "reflections.md not found" in checks[0][2]


def test_growth_doctor_checks_error_is_single_warn():
    checks = growth_doctor_checks({"error": "boom"})
    assert checks == [("Memory growth (B0)", "WARN", "measurement error: boom")]


def test_format_growth_lines_readable(cfg):
    _write(cfg, FIXTURE_REFLECTIONS)
    lines = format_growth_lines(measure_memory_growth(cfg, now=NOW))
    text = "\n".join(lines)
    assert text.startswith("memory growth (B0):")
    assert "total memory:" in text
    assert "fattest sections" in text
    assert "coldest sections" in text
    assert "coldness unknown: 1 section(s)" in text
