"""Derived startup memory files for compact session priming."""

from __future__ import annotations

import re

from .config import Config

_SECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^## |\Z)"
_SUBSECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^### |\Z)"


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

    for heading in (
        "## Core Identity",
        "## Preferences & Opinions",
        "## Relationship & Communication",
    ):
        section = _extract_h2_section(reflections, heading)
        if section:
            parts.extend(["", section.strip()])

    key_facts = _extract_h2_section(reflections, "## Key Facts & Context")
    if key_facts:
        filtered = _filter_priority_bullets(key_facts, allowed_prefixes=("- 🔴",))
        if filtered:
            parts.extend(["", filtered.strip()])

    return "\n".join(parts)


def _build_active(reflections: str, observations: str) -> str:
    parts = [
        "# Active Context",
        "",
        "<!-- Auto-maintained from reflections.md and observations.md. -->",
    ]

    for heading in ("## Active Projects", "## Recent Themes"):
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
