"""Derived startup memory files for compact session priming."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_SECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^## |\Z)"
_SUBSECTION_RE_TEMPLATE = r"(?ms)^({heading}\n.*?)(?=^### |\Z)"
DEFAULT_STARTUP_BUDGET_CHARS = 24000
MIN_STARTUP_BUDGET_CHARS = 2000
LARGE_STARTUP_CHUNK_CHARS = 8000
STARTUP_PROFILE_PREFERENCE_LIMIT = 16
_OM_METADATA_COMMENT_RE = re.compile(r"\s*<!--om:.*?-->")


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
    chunks = _startup_chunks(config, cwd=cwd, task=task, agent=agent, project_startup=True, strip_metadata=True)
    header = _startup_header(budget=budget, cwd=cwd, task=task, agent=agent)
    footer = _recall_footer(task=task, cwd=cwd)

    used = len(header) + len(footer)
    selected: list[StartupChunk] = []
    overflow: list[StartupChunk] = []
    for chunk in sorted(chunks, key=lambda item: (-item.priority, item.source, item.heading, item.handle)):
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
    if route_terms and any(term in lower_body or term in lower_heading for term in route_terms):
        priority += 5
    return priority


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


def _route_terms(*, cwd: str | None, task: str | None, agent: str | None) -> list[str]:
    terms: list[str] = []
    if cwd:
        path = Path(cwd)
        terms.extend(part.lower() for part in (path.name, path.parent.name) if part)
    if task:
        terms.extend(_keyword_terms(task))
    if agent:
        terms.append(agent.lower())
    return [term for term in terms if len(term) >= 3]


def _keyword_terms(value: str) -> list[str]:
    stop = {"the", "and", "for", "with", "from", "this", "that", "into", "issue", "issues"}
    return [term for term in re.findall(r"[a-zA-Z0-9_.-]+", value.lower()) if len(term) >= 3 and term not in stop]


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
    import os

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
