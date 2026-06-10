"""B0 growth measurement for durable memory documents (v0.8.0 Gate 6).

Pure, read-only instrumentation: measures how big the durable memory documents
are (per document, per H2 section, per H3 subsection of ``reflections.md``) and
how *cold* each section is — the age of the most recent timestamp recoverable
from its own content. Nothing here writes, mutates, or guesses: a section with
no recoverable timestamp reports coldness as unknown (``None``), never an
estimate. The report exists so a future compaction decision is data-grounded.

Coldness signals (in the order they actually appear in the files):

1. The Gate 3 section provenance stamp's ``last_reflected=<date>`` and the end
   of its ``derived_from_obs_window=<min>..<max>`` range
   (``<!--om-section: ...-->``).
2. Per-bullet inline metadata timestamps ``last_seen=`` / ``last_verified=``
   (``<!--om: ...-->``; ``expires=`` is deliberately excluded — a future expiry
   is not activity).
3. A date embedded in a heading (e.g. observations-style ``## 2026-06-01``).

The newest of these wins. All failure modes degrade to best-effort numbers:
missing files yield an empty-but-valid report, non-UTF-8 bytes are decoded with
replacement, malformed timestamps are skipped, and the public entry point never
lets an exception escape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

DEFAULT_TOP_N = 3

_BULLET_RE = re.compile(r"^\s*[-*]\s+\S")
_H2_RE = re.compile(r"^## (.+?)\s*$")
_H3_RE = re.compile(r"^### (.+?)\s*$")
_HEADING_DATE_RE = re.compile(r"^#{1,6}\s.*?(\d{4}-\d{2}-\d{2})\b")
# last_seen/last_verified are full ISO timestamps (optionally with a trailing Z);
# last_reflected is a bare date. The time part requires a literal "T" (the only
# separator OM's _iso emits) — allowing a space here would let a bare date
# greedily swallow a following space-separated field such as
# "last_reflected=<date> derived_from_obs_window=<date>..<date>" and lose the
# signal. The pattern ends on a digit or Z so the comment's "-->" never matches.
_STAMP_TS_RE = re.compile(r"\b(?:last_reflected|last_seen|last_verified)=(\d{4}-\d{2}-\d{2}(?:T[\d:.+\-]*\d)?Z?)")
_OBS_WINDOW_END_RE = re.compile(r"\bderived_from_obs_window=\d{4}-\d{2}-\d{2}\.\.(\d{4}-\d{2}-\d{2})\b")


@dataclass(frozen=True)
class _DocStats:
    name: str
    path: Path
    exists: bool
    text: str
    n_bytes: int


def measure_memory_growth(
    config: Config,
    *,
    now: datetime | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Measure durable memory document growth. Pure and read-only.

    Returns a JSON-serializable report; never raises and never writes. Missing
    files produce an empty-but-valid report; malformed content is measured
    best-effort.
    """
    try:
        return _measure(config, now=now, top_n=top_n)
    except Exception as exc:  # defensive: measurement must never break a caller
        report = _empty_report()
        report["error"] = str(exc)
        return report


def _empty_report() -> dict:
    return {
        "documents": [],
        "sections": [],
        "fattest_sections": [],
        "coldest_sections": [],
        "unknown_coldness_sections": [],
        "totals": {
            "total_bytes": 0,
            "total_lines": 0,
            "total_bullets": 0,
            "reflections_bytes": 0,
            "section_count": 0,
            "subsection_count": 0,
        },
    }


def _measure(config: Config, *, now: datetime | None, top_n: int) -> dict:
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    top_n = max(int(top_n), 0)

    docs = [
        _read_document("reflections.md", config.reflections_path),
        _read_document("profile.md", config.profile_path),
        _read_document("active.md", config.active_path),
        _read_document("observations.md", config.observations_path),
    ]
    reflections = docs[0]
    sections = _measure_sections(reflections.text, doc_bytes=reflections.n_bytes, now=now_value)

    known_cold = [s for s in sections if s["age_days"] is not None]
    fattest = sorted(sections, key=lambda s: (-s["bytes"], s["heading"]))[:top_n]
    coldest = sorted(known_cold, key=lambda s: (-s["age_days"], s["heading"]))[:top_n]

    report = _empty_report()
    report["documents"] = [
        {
            "name": doc.name,
            "exists": doc.exists,
            "bytes": doc.n_bytes,
            "lines": _line_count(doc.text),
            "bullets": _bullet_count(doc.text),
        }
        for doc in docs
    ]
    report["sections"] = sections
    report["fattest_sections"] = [{"heading": s["heading"], "bytes": s["bytes"], "share": s["share"]} for s in fattest]
    report["coldest_sections"] = [
        {"heading": s["heading"], "age_days": s["age_days"], "last_activity": s["last_activity"]} for s in coldest
    ]
    report["unknown_coldness_sections"] = [s["heading"] for s in sections if s["age_days"] is None]
    report["totals"] = {
        "total_bytes": sum(doc.n_bytes for doc in docs),
        "total_lines": sum(_line_count(doc.text) for doc in docs),
        "total_bullets": sum(_bullet_count(doc.text) for doc in docs),
        "reflections_bytes": reflections.n_bytes,
        "section_count": len(sections),
        "subsection_count": sum(len(s["subsections"]) for s in sections),
    }
    return report


def _read_document(name: str, path: Path) -> _DocStats:
    """Read a memory document without mutating anything. Missing/unreadable -> empty."""
    try:
        raw = path.read_bytes()
    except OSError:
        return _DocStats(name=name, path=path, exists=False, text="", n_bytes=0)
    # Non-UTF-8 bytes must not abort measurement; sizes stay byte-accurate from raw.
    return _DocStats(name=name, path=path, exists=True, text=raw.decode("utf-8", errors="replace"), n_bytes=len(raw))


def _measure_sections(text: str, *, doc_bytes: int, now: datetime) -> list[dict]:
    sections: list[dict] = []
    for heading, lines in _split_h2(text):
        block = "\n".join(lines)
        last_activity = _latest_timestamp(lines)
        sections.append(
            {
                "heading": heading,
                "bytes": _utf8_len(block),
                "lines": len(lines),
                "bullets": _bullet_count(block),
                "share": _share(_utf8_len(block), doc_bytes),
                "last_activity": _date_str(last_activity),
                "age_days": _age_days(last_activity, now),
                "subsections": _measure_subsections(lines, doc_bytes=doc_bytes, now=now),
            }
        )
    return sections


def _measure_subsections(section_lines: list[str], *, doc_bytes: int, now: datetime) -> list[dict]:
    subsections: list[dict] = []
    for heading, lines in _split_h3(section_lines):
        block = "\n".join(lines)
        last_activity = _latest_timestamp(lines)
        subsections.append(
            {
                "heading": heading,
                "bytes": _utf8_len(block),
                "lines": len(lines),
                "bullets": _bullet_count(block),
                "share": _share(_utf8_len(block), doc_bytes),
                "last_activity": _date_str(last_activity),
                "age_days": _age_days(last_activity, now),
            }
        )
    return subsections


def _split_h2(text: str) -> list[tuple[str, list[str]]]:
    """Split into (heading, lines-including-heading) per H2 section.

    Preamble before the first H2 counts toward document totals but is not a
    section. Duplicate headings stay distinct entries.
    """
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _H2_RE.match(line)
        if match:
            current = [line]
            sections.append((match.group(1), current))
        elif current is not None:
            current.append(line)
    return [(heading, _trim_trailing_blanks(lines)) for heading, lines in sections]


def _split_h3(section_lines: list[str]) -> list[tuple[str, list[str]]]:
    subsections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in section_lines:
        match = _H3_RE.match(line)
        if match:
            current = [line]
            subsections.append((match.group(1), current))
        elif current is not None:
            current.append(line)
    return [(heading, _trim_trailing_blanks(lines)) for heading, lines in subsections]


def _trim_trailing_blanks(lines: list[str]) -> list[str]:
    """Drop trailing blank lines so the inter-section separator blank is not
    counted as part of a section's size."""
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _latest_timestamp(lines: list[str]) -> datetime | None:
    """Most recent timestamp recoverable from the lines' own content, else None."""
    latest: datetime | None = None
    for line in lines:
        candidates = _STAMP_TS_RE.findall(line)
        candidates.extend(_OBS_WINDOW_END_RE.findall(line))
        heading_date = _HEADING_DATE_RE.match(line)
        if heading_date:
            candidates.append(heading_date.group(1))
        for raw in candidates:
            parsed = _parse_ts(raw)
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
    return latest


def _parse_ts(value: str) -> datetime | None:
    raw = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_days(ts: datetime | None, now: datetime) -> int | None:
    if ts is None:
        return None
    # A future-dated stamp (clock skew, hand-edited file) is "fresh", never negative.
    return max((now - ts).days, 0)


def _date_str(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d") if ts is not None else None


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole > 0 else 0.0


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _bullet_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if _BULLET_RE.match(line))


def format_bytes(n_bytes: int) -> str:
    """Human-readable size: 412 B / 37.2 KB / 1.4 MB."""
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def growth_doctor_checks(report: dict) -> list[tuple[str, str, str]]:
    """Render the growth report as compact ``om doctor`` rows (name, status, detail).

    Purely informational: every row is PASS unless measurement itself reported
    an internal error, which becomes a single WARN row.
    """
    if report.get("error"):
        return [("Memory growth (B0)", "WARN", f"measurement error: {report['error']}")]

    totals = report.get("totals", {})
    reflections = next((d for d in report.get("documents", []) if d.get("name") == "reflections.md"), None)
    if reflections is None or not reflections.get("exists"):
        summary = f"reflections.md not found; total memory {format_bytes(totals.get('total_bytes', 0))}"
    else:
        summary = (
            f"reflections.md {format_bytes(reflections['bytes'])}, "
            f"{totals.get('section_count', 0)} section(s), "
            f"{totals.get('subsection_count', 0)} subsection(s), "
            f"{reflections.get('bullets', 0)} bullet(s); "
            f"total memory {format_bytes(totals.get('total_bytes', 0))}"
        )
    checks: list[tuple[str, str, str]] = [("Memory growth (B0)", "PASS", summary)]

    fattest = report.get("fattest_sections") or []
    if fattest:
        top = fattest[0]
        checks.append(
            (
                "Memory growth: largest section",
                "PASS",
                f"{top['heading']} — {format_bytes(top['bytes'])} ({top['share']:.0%} of reflections.md)",
            )
        )

    coldest = report.get("coldest_sections") or []
    unknown = report.get("unknown_coldness_sections") or []
    if coldest:
        top = coldest[0]
        detail = f"{top['heading']} — last activity {top['last_activity']} ({top['age_days']}d ago)"
        if unknown:
            detail += f"; coldness unknown for {len(unknown)} section(s)"
        checks.append(("Memory growth: coldest section", "PASS", detail))
    elif unknown:
        checks.append(
            (
                "Memory growth: coldest section",
                "PASS",
                f"no recoverable timestamps; coldness unknown for {len(unknown)} section(s)",
            )
        )
    return checks


def format_growth_lines(report: dict) -> list[str]:
    """Render the growth report as readable lines for the quality-report text output."""
    lines = ["memory growth (B0):"]
    if report.get("error"):
        lines.append(f"  measurement error: {report['error']}")
        return lines
    totals = report.get("totals", {})
    lines.append(
        f"  total memory: {format_bytes(totals.get('total_bytes', 0))} "
        f"({totals.get('total_lines', 0)} lines, {totals.get('total_bullets', 0)} bullets)"
    )
    for doc in report.get("documents", []):
        status = f"{format_bytes(doc['bytes'])}, {doc['lines']} lines, {doc['bullets']} bullets"
        if not doc.get("exists"):
            status = "missing"
        lines.append(f"    {doc['name']}: {status}")
    fattest = report.get("fattest_sections") or []
    if fattest:
        lines.append("  fattest sections (reflections.md):")
        for section in fattest:
            lines.append(f"    {format_bytes(section['bytes']):>10} ({section['share']:.0%})  {section['heading']}")
    coldest = report.get("coldest_sections") or []
    if coldest:
        lines.append("  coldest sections (reflections.md):")
        for section in coldest:
            detail = f"{section['age_days']:>5}d (last activity {section['last_activity']})"
            lines.append(f"    {detail}  {section['heading']}")
    unknown = report.get("unknown_coldness_sections") or []
    if unknown:
        lines.append(f"  coldness unknown: {len(unknown)} section(s) with no recoverable timestamp")
    return lines
