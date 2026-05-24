"""Derived startup memory files for compact session priming."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

_SECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^## |\Z)"
_SUBSECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^### |\Z)"
DEFAULT_STARTUP_BUDGET_CHARS = 24000
MIN_STARTUP_BUDGET_CHARS = 2000
LARGE_STARTUP_CHUNK_CHARS = 8000
STARTUP_PROFILE_PREFERENCE_LIMIT = 16
# Priority boost for a chunk matching the current cwd/task route terms.
_ROUTE_MATCH_BOOST = 6
# Operational facts (tool versions, install status) older than this many days are
# annotated as potentially stale in startup context (OM_STARTUP_FRESHNESS_DAYS).
DEFAULT_STARTUP_FRESHNESS_DAYS = 14
_OM_METADATA_COMMENT_RE = re.compile(r"\s*<!--om:.*?-->")
# Capture an ISO timestamp value, ending on a digit so the trailing "-->" of the
# metadata comment is never swallowed.
_LAST_SEEN_RE = re.compile(r"last_seen=(\d{4}-\d{2}-\d{2}(?:[T ][\d:.+\-]*\d)?)")
# A version-number token or an install/version status word marks a bullet as an
# "operational" fact whose truth decays (the agent can verify it live).
_VERSION_TOKEN_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_OPERATIONAL_WORD_RE = re.compile(
    r"\b(installed|install status|version|upgraded|downgraded|running|available|enabled|disabled|up to date)\b",
    re.IGNORECASE,
)
_FRESHNESS_MARKER = "as of"
_KIND_RE = re.compile(r"\bkind=(\w+)")
# Durable fact kinds whose truth does not decay — never freshness-marked even if
# their visible text happens to contain a version-like token.
_DURABLE_KINDS = frozenset({"preference", "identity", "policy", "mode", "evergreen"})
_FRESHNESS_MARKER_RE = re.compile(r"\s*\(as of \d{4}-\d{2}-\d{2} — verify\)")


@dataclass(frozen=True)
class StartupChunk:
    source: str
    heading: str
    body: str
    handle: str
    priority: int

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class StartupPayload:
    text: str
    budget_chars: int
    included_handles: list[str]
    overflow: list[dict[str, str | int]]

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "budget_chars": self.budget_chars,
            "included_handles": self.included_handles,
            "overflow": self.overflow,
        }


def ensure_startup_memory(config: Config | None = None) -> None:
    """Refresh compact startup memory files when sources are missing or stale."""
    if config is None:
        config = Config()

    if not _startup_memory_needs_refresh(config):
        return

    refresh_startup_memory(config)


def refresh_startup_memory(config: Config | None = None) -> None:
    """Regenerate compact startup memory files from reflections and observations."""
    if config is None:
        config = Config()

    config.ensure_memory_dir()

    reflections = config.reflections_path.read_text() if config.reflections_path.exists() else ""
    observations = config.observations_path.read_text() if config.observations_path.exists() else ""

    config.profile_path.write_text(_build_profile(reflections).rstrip() + "\n")
    config.active_path.write_text(_build_active(reflections, observations).rstrip() + "\n")


def build_startup_payload(
    config: Config,
    *,
    budget_chars: int | None = None,
    cwd: str | None = None,
    task: str | None = None,
    agent: str | None = None,
) -> StartupPayload:
    """Build a deterministic, budgeted startup payload with recall handles."""
    ensure_startup_memory(config)
    budget = max(int(budget_chars or DEFAULT_STARTUP_BUDGET_CHARS), MIN_STARTUP_BUDGET_CHARS)
    # Keep metadata so freshness can be computed across all sections, then applied
    # to the surviving (de-duplicated) copy using the freshest last_seen anywhere.
    chunks = _startup_chunks(config, cwd=cwd, task=task, agent=agent, project_startup=True, strip_metadata=False)
    fresh_map = _operational_freshness_map(chunks)
    header = _startup_header(budget=budget, cwd=cwd, task=task, agent=agent)
    footer = _recall_footer(task=task, cwd=cwd)

    # De-duplicate bullets across sections in priority order (the highest-priority
    # section keeps each bullet) before spending budget on repeats; then annotate
    # freshness and strip metadata.
    ordered = sorted(chunks, key=lambda item: (-item.priority, item.source, item.heading, item.handle))
    ordered, _removed = _dedupe_startup_chunks(ordered)
    now = datetime.now(timezone.utc)
    days = _freshness_days()
    ordered = [
        replace(chunk, body=_annotate_and_strip(chunk.body, fresh_map, now=now, freshness_days=days))
        for chunk in ordered
    ]

    used = len(header) + len(footer)
    selected: list[StartupChunk] = []
    overflow: list[StartupChunk] = []
    for chunk in ordered:
        if not _has_visible_content(chunk.body):
            continue  # emptied by dedup — nothing left to show
        chunk_text = "\n\n" + chunk.body.strip()
        if used + len(chunk_text) <= budget:
            selected.append(chunk)
            used += len(chunk_text)
        else:
            overflow.append(chunk)

    selected.sort(key=_selected_chunk_order)
    parts = [header.rstrip()]
    parts.extend(chunk.body.strip() for chunk in selected)
    if overflow:
        parts.append(_overflow_section(overflow))
    parts.append(footer.rstrip())
    text = "\n\n".join(part for part in parts if part).rstrip() + "\n"
    if len(text) > budget:
        text = _hard_trim(text, budget)
    return StartupPayload(
        text=text,
        budget_chars=budget,
        included_handles=[chunk.handle for chunk in selected],
        overflow=[
            {"handle": chunk.handle, "heading": chunk.heading, "source": chunk.source, "chars": chunk.size}
            for chunk in overflow
        ],
    )


def recall_handle(config: Config, handle: str) -> str:
    """Expand a startup recall handle into Markdown."""
    ensure_startup_memory(config)
    if handle == "startup:profile":
        return config.profile_path.read_text() if config.profile_path.exists() else ""
    if handle == "startup:active":
        return config.active_path.read_text() if config.active_path.exists() else ""
    chunks = _startup_chunks(config)
    for chunk in chunks:
        if chunk.handle == handle:
            return chunk.body.rstrip() + "\n"
    startup_chunks = _startup_chunks(config, project_startup=True)
    for chunk in startup_chunks:
        if chunk.handle == handle:
            return chunk.body.rstrip() + "\n"
    raise KeyError(handle)


def _startup_memory_needs_refresh(config: Config) -> bool:
    """Return True when compact startup files are missing or older than source files."""
    if not config.profile_path.exists() or not config.active_path.exists():
        return True

    profile_mtime = config.profile_path.stat().st_mtime
    active_mtime = config.active_path.stat().st_mtime

    source_paths = [p for p in (config.reflections_path, config.observations_path) if p.exists()]
    if not source_paths:
        return False

    latest_source_mtime = max(p.stat().st_mtime for p in source_paths)
    return profile_mtime < latest_source_mtime or active_mtime < latest_source_mtime


def _build_profile(reflections: str) -> str:
    parts = [
        "# Startup Profile",
        "",
        "<!-- Auto-maintained from reflections.md. -->",
    ]

    for heading in _enabled_profile_headings():
        section = _extract_h2_section(reflections, heading)
        if section:
            parts.extend(["", section.strip()])

    key_facts = _extract_h2_section(reflections, "## Key Facts & Context")
    if key_facts and "key-facts" in _enabled_profile_section_keys():
        filtered = _filter_priority_bullets(key_facts, allowed_prefixes=("- 🔴",))
        if filtered:
            parts.extend(["", filtered.strip()])

    # Materialized files stay raw (with metadata); freshness is applied at
    # payload-build time so it always reflects the current OM_STARTUP_FRESHNESS_DAYS.
    return "\n".join(parts)


def _build_active(reflections: str, observations: str) -> str:
    parts = [
        "# Active Context",
        "",
        "<!-- Auto-maintained from reflections.md and observations.md. -->",
    ]

    for heading in ("## Active Projects", "## Life & Operations", "## Creative & Professional", "## Recent Themes"):
        section = _extract_h2_section(reflections, heading)
        if section:
            parts.extend(["", section.strip()])

    latest_context = _extract_latest_current_context(observations)
    if latest_context:
        parts.extend(["", latest_context.strip()])

    return "\n".join(parts)


def _extract_h2_section(text: str, heading: str) -> str:
    if not text:
        return ""
    pattern = _SECTION_RE_TEMPLATE.format(heading=re.escape(heading))
    match = re.search(pattern, text)
    return match.group(1).rstrip() if match else ""


def _extract_h3_subsection(text: str, heading: str) -> str:
    if not text:
        return ""
    pattern = _SUBSECTION_RE_TEMPLATE.format(heading=re.escape(heading))
    match = re.search(pattern, text)
    return match.group(1).rstrip() if match else ""


# --- #50 startup quality: freshness, dedup, quality report ---


def _freshness_days() -> int:
    try:
        days = int(os.environ.get("OM_STARTUP_FRESHNESS_DAYS", str(DEFAULT_STARTUP_FRESHNESS_DAYS)))
    except ValueError:
        return DEFAULT_STARTUP_FRESHNESS_DAYS
    return days if days >= 0 else DEFAULT_STARTUP_FRESHNESS_DAYS


def _parse_iso_ts(value: str) -> datetime | None:
    raw = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _looks_operational(visible: str) -> bool:
    """True for tool-version / install-status style facts whose truth decays."""
    return bool(_VERSION_TOKEN_RE.search(visible) or _OPERATIONAL_WORD_RE.search(visible))


def _split_visible_and_metadata(line: str) -> tuple[str, str]:
    match = _OM_METADATA_COMMENT_RE.search(line)
    if not match:
        return line, ""
    return line[: match.start()], line[match.start() :]


def _operational_last_seen(line: str) -> datetime | None:
    """Return the last_seen of a non-durable operational bullet, else None."""
    if not _is_bullet(line):
        return None
    seen = _LAST_SEEN_RE.search(line)
    if not seen:
        return None
    # Respect the authoritative kind= tag: durable facts never decay, even if
    # their text contains a version-like token (e.g. "prefers Python 3.11").
    kind = _KIND_RE.search(line)
    if kind and kind.group(1).lower() in _DURABLE_KINDS:
        return None
    visible, _metadata = _split_visible_and_metadata(line)
    if not _looks_operational(visible):
        return None
    return _parse_iso_ts(seen.group(1))


def _operational_freshness_map(chunks: list[StartupChunk]) -> dict[str, datetime]:
    """Map each operational fact (normalized) to its FRESHEST last_seen across sections.

    Cross-section duplicates of the same fact may carry different last_seen
    values; the fact is only as stale as its most recent sighting anywhere.
    """
    fresh: dict[str, datetime] = {}
    for chunk in chunks:
        for line in chunk.body.split("\n"):
            last_seen = _operational_last_seen(line)
            if last_seen is None:
                continue
            key = _normalize_bullet(line)
            if key and (key not in fresh or last_seen > fresh[key]):
                fresh[key] = last_seen
    return fresh


def _annotate_and_strip(body: str, fresh_map: dict[str, datetime], *, now: datetime, freshness_days: int) -> str:
    """Annotate stale operational bullets (using the global freshest last_seen) and strip metadata."""
    out: list[str] = []
    for line in body.split("\n"):
        out.append(_annotate_line_with_map(line, fresh_map, now=now, freshness_days=freshness_days))
    return _strip_om_metadata("\n".join(out))


def _annotate_line_with_map(line: str, fresh_map: dict[str, datetime], *, now: datetime, freshness_days: int) -> str:
    if not _is_bullet(line):
        return line
    visible, metadata = _split_visible_and_metadata(line)
    if _FRESHNESS_MARKER in visible.lower() or not _looks_operational(visible):
        return line
    last_seen = fresh_map.get(_normalize_bullet(line))
    if last_seen is None or (now - last_seen).days < freshness_days:
        return line
    marker = f" ({_FRESHNESS_MARKER} {last_seen.strftime('%Y-%m-%d')} — verify)"
    return visible.rstrip() + marker + metadata


def _normalize_bullet(line: str) -> str:
    """Normalize a bullet for cross-section duplicate detection.

    Strips the list marker, priority emoji, markdown emphasis, any metadata
    comment, and the freshness marker, then casefolds and collapses whitespace.
    The freshness marker is stripped so the same fact dedupes whether or not one
    copy was annotated stale (different last_seen across sections).
    """
    visible, _metadata = _split_visible_and_metadata(line)
    text = _FRESHNESS_MARKER_RE.sub("", visible).strip()
    text = re.sub(r"^[-*]\s+", "", text)  # list marker
    text = re.sub(r"[🔴🟡🟢⚪️🔵🟠]", "", text)  # priority dots
    text = text.replace("*", "").replace("`", "")  # markdown emphasis/code
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def _is_bullet(line: str) -> bool:
    return line.lstrip().startswith(("- ", "* "))


def _has_visible_content(body: str) -> bool:
    """True if the body has any non-heading, non-blank line (survives dedup)."""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def startup_quality_report(
    config: Config,
    *,
    budget_chars: int | None = None,
    cwd: str | None = None,
    task: str | None = None,
    agent: str | None = None,
) -> dict:
    """Diagnostic for ``om context --quality-report``.

    Reports cross-section duplicate bullets dropped from the payload, operational
    facts (tool versions / install status) that look stale, and budget usage per
    included section.
    """
    ensure_startup_memory(config)
    budget = max(int(budget_chars or DEFAULT_STARTUP_BUDGET_CHARS), MIN_STARTUP_BUDGET_CHARS)
    payload = build_startup_payload(config, budget_chars=budget, cwd=cwd, task=task, agent=agent)

    stripped = _startup_chunks(config, cwd=cwd, task=task, agent=agent, project_startup=True, strip_metadata=True)
    ordered = sorted(stripped, key=lambda item: (-item.priority, item.source, item.heading, item.handle))
    deduped, removed = _dedupe_startup_chunks(ordered)

    now = datetime.now(timezone.utc)
    days = _freshness_days()
    raw_chunks = _startup_chunks(config, cwd=cwd, task=task, agent=agent, project_startup=True, strip_metadata=False)
    # Use the same global freshest-last_seen logic as the payload, so the report
    # agrees with what's actually annotated, and list each fact once.
    fresh_map = _operational_freshness_map(raw_chunks)
    stale: list[dict] = []
    reported: set[str] = set()
    for chunk in raw_chunks:
        for line in chunk.body.split("\n"):
            if _operational_last_seen(line) is None:
                continue
            key = _normalize_bullet(line)
            if not key or key in reported:
                continue
            freshest = fresh_map.get(key)
            if freshest is None or (now - freshest).days < days:
                continue
            reported.add(key)
            visible, _metadata = _split_visible_and_metadata(line)
            clean = _FRESHNESS_MARKER_RE.sub("", re.sub(r"^[-*]\s+", "", visible.strip())).strip()
            stale.append(
                {
                    "section": chunk.heading,
                    "text": clean,
                    "as_of": freshest.strftime("%Y-%m-%d"),
                    "age_days": (now - freshest).days,
                }
            )

    included = set(payload.included_handles)
    by_section = [
        {"handle": chunk.handle, "heading": chunk.heading, "chars": len(chunk.body.strip())}
        for chunk in deduped
        if chunk.handle in included
    ]
    return {
        "budget_chars": budget,
        "used_chars": len(payload.text),
        "duplicate_bullets": sorted(set(removed)),
        "duplicate_count": len(removed),
        "stale_operational_facts": stale,
        "budget_by_section": by_section,
        "overflow_handles": [item["handle"] for item in payload.overflow],
    }


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _next_nonblank_indent(lines: list[str], idx: int) -> int:
    for j in range(idx + 1, len(lines)):
        if lines[j].strip():
            return _line_indent(lines[j])
    return 0


def _dedupe_startup_chunks(chunks: list[StartupChunk]) -> tuple[list[StartupChunk], list[str]]:
    """Drop bullets already shown in an earlier (higher-priority) chunk.

    ``chunks`` must be in selection order (highest priority first). Returns the
    de-duplicated chunks plus the list of removed (normalized) duplicate bullets
    for the quality report. Only *top-level leaf* bullets are de-duplicated:
    bullets with nested children are kept intact so dedup can never orphan a
    sub-bullet. Headings, prose, and nested bullets are preserved.
    """
    seen: set[str] = set()
    removed: list[str] = []
    out: list[StartupChunk] = []
    for chunk in chunks:
        kept_lines: list[str] = []
        lines = chunk.body.split("\n")
        for i, line in enumerate(lines):
            if _is_bullet(line) and _line_indent(line) == 0 and _next_nonblank_indent(lines, i) == 0:
                # top-level leaf bullet (no nested children)
                norm = _normalize_bullet(line)
                if norm and norm in seen:
                    removed.append(norm)
                    continue
                if norm:
                    seen.add(norm)
            kept_lines.append(line)
        out.append(replace(chunk, body="\n".join(kept_lines)))
    return out, removed


def _filter_priority_bullets(section: str, allowed_prefixes: tuple[str, ...]) -> str:
    lines = section.splitlines()
    if not lines:
        return ""

    kept = [lines[0]]
    keep_current = False
    for line in lines[1:]:
        stripped = line.lstrip()
        if any(stripped.startswith(prefix) for prefix in allowed_prefixes):
            kept.append(line)
            keep_current = True
            continue

        if keep_current and (line.startswith("  ") or line.startswith("\t")):
            kept.append(line)
            continue

        keep_current = False

    return "\n".join(kept) if len(kept) > 1 else ""


def _extract_latest_current_context(observations: str) -> str:
    if not observations:
        return ""

    date_matches = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2})$", observations, flags=re.MULTILINE))
    if not date_matches:
        return ""

    latest = max(date_matches, key=lambda match: match.group(1))
    start = latest.start()
    next_match = next((m for m in date_matches if m.start() > start), None)
    end = next_match.start() if next_match else len(observations)
    date_section = observations[start:end].rstrip()

    current_context = _extract_h3_subsection(date_section, "### Current Context")
    if not current_context:
        return ""

    current_context_lines = current_context.splitlines()
    if current_context_lines and current_context_lines[0].strip() == "### Current Context":
        current_context = "\n".join(current_context_lines[1:]).strip()

    return "\n".join(
        [
            "## Current Session Snapshot",
            "",
            f"### {latest.group(1)}",
            "",
            current_context,
        ]
    )


def _startup_chunks(
    config: Config,
    *,
    cwd: str | None = None,
    task: str | None = None,
    agent: str | None = None,
    project_startup: bool = False,
    strip_metadata: bool = False,
) -> list[StartupChunk]:
    chunks: list[StartupChunk] = []
    for source, path in (("profile", config.profile_path), ("active", config.active_path)):
        text = path.read_text() if path.exists() else ""
        chunk_parts: list[tuple[str, str, str]]
        if project_startup and source == "profile":
            chunk_parts = _startup_profile_chunk_parts(text, cwd=cwd, task=task, agent=agent)
        elif project_startup and source == "active":
            chunk_parts = _active_startup_chunk_parts(text)
        else:
            chunk_parts = [
                (heading, body, f"startup:{source}:{_slug(heading)}") for heading, body in _split_h2_chunks(text)
            ]
        for heading, body, handle in chunk_parts:
            if strip_metadata:
                body = _strip_om_metadata(body)
            if project_startup and _is_empty_startup_chunk(body):
                continue
            chunks.append(
                StartupChunk(
                    source=source,
                    heading=heading,
                    body=body,
                    handle=handle,
                    priority=_chunk_priority(source, heading, body, cwd=cwd, task=task, agent=agent),
                )
            )
    return chunks


def _startup_profile_chunk_parts(
    text: str,
    *,
    cwd: str | None,
    task: str | None,
    agent: str | None,
) -> list[tuple[str, str, str]]:
    normal_chunks = [(heading, body, f"startup:profile:{_slug(heading)}") for heading, body in _split_h2_chunks(text)]
    by_heading = {heading: body for heading, body, _handle in normal_chunks}
    core = by_heading.get("Core Identity", "")
    preferences = by_heading.get("Preferences & Opinions", "")
    core_has_nested_preferences = bool(_profile_nested_lines(core, label="Preferences"))
    large_core = len(core) > LARGE_STARTUP_CHUNK_CHARS or core_has_nested_preferences
    large_preferences = len(preferences) > LARGE_STARTUP_CHUNK_CHARS
    if not large_core and not large_preferences:
        return normal_chunks

    working_profile = _build_working_profile_chunk(
        core=core,
        preferences=preferences,
        include_basics=large_core,
        cwd=cwd,
        task=task,
        agent=agent,
    )
    projected: list[tuple[str, str, str]] = []
    if working_profile:
        projected.append(("Working Profile", working_profile, "startup:profile:working-profile"))
    for heading, body, handle in normal_chunks:
        if heading == "Core Identity" and large_core:
            continue
        if heading == "Preferences & Opinions" and large_preferences:
            continue
        projected.append((heading, body, handle))
    return projected


def _build_working_profile_chunk(
    *,
    core: str,
    preferences: str,
    include_basics: bool,
    cwd: str | None,
    task: str | None,
    agent: str | None,
) -> str:
    basics = _profile_identity_basics(core) if include_basics else []
    preference_lines = _profile_nested_lines(core, label="Preferences") + _profile_section_bullets(preferences)
    selected_preferences = _select_startup_profile_lines(
        preference_lines,
        limit=STARTUP_PROFILE_PREFERENCE_LIMIT,
        route_terms=_route_terms(cwd=cwd, task=task, agent=agent),
    )
    if not basics and not selected_preferences:
        return ""

    parts = ["## Working Profile"]
    if basics:
        parts.extend(["", *basics])
    if selected_preferences:
        parts.extend(["", "- **Startup working contract:**"])
        parts.extend(f"  {line}" for line in selected_preferences)
    parts.extend(
        [
            "",
            "- Full profile available with `om recall --handle startup:profile`.",
        ]
    )
    return "\n".join(parts)


def _profile_identity_basics(core: str) -> list[str]:
    if not core:
        return []
    wanted = ("- **Name:**", "- **Role/occupation:**", "- **Communication style:**", "- **Working hours:**")
    lines: list[str] = []
    for line in core.splitlines()[1:]:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in wanted):
            lines.append(stripped)
    return lines


def _profile_nested_lines(section: str, *, label: str) -> list[str]:
    if not section:
        return []
    lines: list[str] = []
    collecting = False
    marker = f"- **{label}:**"
    for line in section.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("- **") and not stripped.startswith(marker):
            collecting = False
        if stripped.startswith(marker):
            collecting = True
            continue
        if collecting and stripped.startswith("- "):
            lines.append(stripped)
    return lines


def _profile_section_bullets(section: str) -> list[str]:
    if not section:
        return []
    return [line.strip() for line in section.splitlines()[1:] if line.strip().startswith("- ")]


def _select_startup_profile_lines(lines: list[str], *, limit: int, route_terms: list[str]) -> list[str]:
    if not lines:
        return []
    indexed = list(enumerate(dict.fromkeys(lines)))
    ranked = sorted(indexed, key=lambda item: (-_startup_profile_line_score(item[1], route_terms), item[0]))
    return [line for _index, line in ranked[:limit]]


def _startup_profile_line_score(line: str, route_terms: list[str]) -> int:
    lower = line.lower()
    score = 1 if "🔴" in line else 0
    score += sum(4 for term in route_terms if term in lower)
    return score


def _active_startup_chunk_parts(text: str) -> list[tuple[str, str, str]]:
    chunk_parts: list[tuple[str, str, str]] = []
    for heading, body in _split_h2_chunks(text):
        subsections = _split_h3_subchunks(heading, body)
        if subsections:
            chunk_parts.extend(subsections)
        else:
            chunk_parts.append((heading, body, f"startup:active:{_slug(heading)}"))
    return chunk_parts


def _split_h3_subchunks(parent_heading: str, body: str) -> list[tuple[str, str, str]]:
    lines = body.splitlines()
    current_heading = ""
    current: list[str] | None = None
    chunks: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("### "):
            current_heading = line[4:].strip()
            current = [line]
            chunks.append((current_heading, current))
        elif current is not None:
            current.append(line)
    if not chunks:
        return []
    return [
        (
            f"{parent_heading} / {heading}",
            "\n".join([f"## {parent_heading} / {heading}", *chunk_lines[1:]]).strip(),
            f"startup:active:{_slug(parent_heading)}:{_slug(heading)}",
        )
        for heading, chunk_lines in chunks
        if "\n".join(chunk_lines).strip()
    ]


def _strip_om_metadata(text: str) -> str:
    return _OM_METADATA_COMMENT_RE.sub("", text)


def _is_empty_startup_chunk(text: str) -> bool:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped == "-" or stripped.startswith("<!--"):
            continue
        return False
    return True


def _split_h2_chunks(text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return []
    lines = text.splitlines()
    preamble: list[str] = []
    chunks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    current_heading = ""
    for line in lines:
        if line.startswith("## "):
            current_heading = line[3:].strip()
            current = [line]
            chunks.append((current_heading, current))
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if preamble and chunks:
        chunks[0][1][:0] = [line for line in preamble if line.strip()] + [""]
    elif preamble:
        return [("root", "\n".join(preamble).strip())]
    return [(heading, "\n".join(body).strip()) for heading, body in chunks if "\n".join(body).strip()]


def _chunk_priority(
    source: str,
    heading: str,
    body: str,
    *,
    cwd: str | None,
    task: str | None,
    agent: str | None,
) -> int:
    lower_heading = heading.lower()
    lower_body = body.lower()
    priority = 4
    if source == "profile":
        priority = 7
    if lower_heading == "working profile":
        priority = 14
    if any(term in lower_heading for term in ("preference", "relationship", "communication")):
        priority = 10
    if "core identity" in lower_heading:
        priority = 8
    if "current session" in lower_heading:
        priority = 9
    if "active projects" in lower_heading:
        priority = 8
    if "life & operations" in lower_heading:
        priority = 6
    if "creative & professional" in lower_heading:
        priority = 3
    route_terms = _route_terms(cwd=cwd, task=task, agent=agent)
    # Match with separators normalized to spaces on both sides, so a cwd slug like
    # "observational-memory" matches a human heading like "Observational Memory".
    hay = _normalize_route_text(lower_heading + " " + lower_body)
    if route_terms and any(_normalize_route_text(term) in hay for term in route_terms):
        # The project/section matching the current cwd/task gets first claim on
        # budget; unmatched active-project inventory overflows to recall handles.
        priority += _ROUTE_MATCH_BOOST
    return priority


def _normalize_route_text(text: str) -> str:
    """Lowercase and collapse separators so hyphen/underscore slugs match spaced text."""
    return re.sub(r"[\s_\-.]+", " ", text.lower()).strip()


def _selected_chunk_order(chunk: StartupChunk) -> tuple[int, int, str, str]:
    source_order = 0 if chunk.source == "profile" else 1
    heading = chunk.heading.lower()
    if chunk.source == "profile":
        if heading == "working profile":
            section_order = 0
        elif "core identity" in heading:
            section_order = 1
        elif "preference" in heading:
            section_order = 2
        elif "relationship" in heading or "communication" in heading:
            section_order = 3
        else:
            section_order = 4
    elif heading.startswith("current session"):
        section_order = 0
    elif heading.startswith("active projects"):
        section_order = 1
    elif heading.startswith("life & operations"):
        section_order = 2
    elif heading.startswith("creative & professional"):
        section_order = 3
    else:
        section_order = 4
    return (source_order, section_order, chunk.heading, chunk.handle)


# Generic directory / filler words that would match almost anything and must not
# drive cwd/task routing.
_GENERIC_ROUTE_TERMS = frozenset(
    {
        "code",
        "src",
        "lib",
        "app",
        "apps",
        "project",
        "projects",
        "experiments",
        "experiment",
        "repo",
        "repos",
        "work",
        "dev",
        "tmp",
        "temp",
        "home",
        "users",
        "user",
        "documents",
        "desktop",
        "github",
        "gitlab",
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "into",
        "issue",
        "issues",
        "task",
        "work",
        "main",
        "master",
    }
)


def _route_terms(*, cwd: str | None, task: str | None, agent: str | None) -> list[str]:
    terms: list[str] = []
    if cwd:
        path = Path(cwd)
        terms.extend(part.lower() for part in (path.name, path.parent.name) if part)
    if task:
        terms.extend(_keyword_terms(task))
    if agent:
        terms.append(agent.lower())
    return [term for term in terms if len(term) >= 3 and term not in _GENERIC_ROUTE_TERMS]


def _keyword_terms(value: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[a-zA-Z0-9_.-]+", value.lower())
        if len(term) >= 3 and term not in _GENERIC_ROUTE_TERMS
    ]


def _startup_header(*, budget: int, cwd: str | None, task: str | None, agent: str | None) -> str:
    lines = [
        "# Observational Memory Startup Context",
        "",
        "<!-- Budgeted startup payload. Generated Markdown remains a materialized view; "
        "expand with recall handles. -->",
        "",
        "## Startup Routing",
        "",
        f"- Budget: {budget} chars",
    ]
    if agent:
        lines.append(f"- Agent: {agent}")
    if cwd:
        lines.append(f"- CWD: {cwd}")
    if task:
        lines.append(f"- Task: {task}")
    return "\n".join(lines)


def _recall_footer(*, task: str | None, cwd: str | None) -> str:
    query = task or (Path(cwd).name if cwd else "current work")
    return "\n".join(
        [
            "## Recall",
            "",
            "- Expand all profile context: `om recall --handle startup:profile`",
            "- Expand all active context: `om recall --handle startup:active`",
            f'- Search deeper memory: `om recall --query "{_shell_safe_hint(query)}" --limit 8`',
        ]
    )


def _overflow_section(chunks: list[StartupChunk]) -> str:
    lines = ["## Startup Overflow", "", "The following context was omitted from the startup budget:"]
    for chunk in chunks:
        lines.append(f"- `{chunk.handle}` ({chunk.size} chars, {chunk.source}: {chunk.heading})")
    return "\n".join(lines)


def _hard_trim(text: str, budget: int) -> str:
    marker = (
        "\n\n## Startup Payload Truncated\n\n- Increase `--budget-chars` or use `om recall` handles for expansion.\n"
    )
    keep = max(budget - len(marker), 0)
    return text[:keep].rstrip() + marker


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "root"


def _shell_safe_hint(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _enabled_profile_section_keys() -> set[str]:
    explicit = os.environ.get("OM_PROFILE_SECTIONS")
    if explicit:
        return {item.strip().lower() for item in explicit.split(",") if item.strip()}
    keys = {"core-identity", "preferences", "relationship", "key-facts"}
    if os.environ.get("OM_PROFILE_INCLUDE_IDENTITY", "1").strip().lower() in {"0", "false", "no", "off"}:
        keys.discard("core-identity")
    return keys


def _enabled_profile_headings() -> tuple[str, ...]:
    keys = _enabled_profile_section_keys()
    headings = []
    if "core-identity" in keys or "identity" in keys:
        headings.append("## Core Identity")
    if "preferences" in keys or "preferences-opinions" in keys:
        headings.append("## Preferences & Opinions")
    if "relationship" in keys or "communication" in keys:
        headings.append("## Relationship & Communication")
    return tuple(headings)
