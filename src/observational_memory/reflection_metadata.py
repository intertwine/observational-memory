"""Inline metadata helpers for reflection entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

_META_RE = re.compile(r"\s*<!--om:\s*(.*?)\s*-->\s*$")
_BULLET_RE = re.compile(r"^(\s*[-*]\s+)(.*?)(\s*<!--om:\s*.*?\s*-->\s*)?$")
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
# A section-level provenance marker. Distinct prefix from `<!--om:` so the
# per-bullet pass (`_META_RE`/`_BULLET_RE`/`parse_metadata`) never sees it: a
# section marker is invisible to `ensure_reflection_metadata` (falls through the
# `if not bullet` branch as a verbatim passthrough) and to the line-based cluster
# and host scope filters (`parse_metadata` returns `{}` for it, so it carries no
# `scope` key and can never be dropped). It lives on its own line immediately
# after a `## ` heading so it rides inside `Section.text` and reassembles
# byte-for-byte; it can never match `_H2_RE`, so it creates no spurious section.
_SECTION_META_RE = re.compile(r"^\s*<!--om-section:\s*(.*?)\s*-->\s*$")


@dataclass(frozen=True)
class PruneSummary:
    pruned: int = 0
    annotated: int = 0
    stale_sectioned: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "pruned": self.pruned,
            "annotated": self.annotated,
            "stale_sectioned": self.stale_sectioned,
        }


@dataclass(frozen=True)
class ReflectionConflict:
    section: str
    kind: str
    actionability: str
    entries: list[dict[str, str]]

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "kind": self.kind,
            "actionability": self.actionability,
            "entries": self.entries,
        }


def ensure_reflection_metadata(
    text: str,
    *,
    now: datetime | None = None,
    node: str = "local",
    source_mtime: datetime | None = None,
) -> str:
    now_value = _iso(now or datetime.now(timezone.utc))
    legacy_seen_value = _iso(source_mtime) if source_mtime is not None else now_value
    lines = text.splitlines()
    current_section = ""
    output: list[str] = []
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            current_section = heading.group(2).strip()
            output.append(line)
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            output.append(line)
            continue
        prefix, body, _comment = bullet.groups()
        body = body.rstrip()
        if not body:
            output.append(line)
            continue
        fields = parse_metadata(line)
        kind = fields.setdefault("kind", infer_kind(body, current_section))
        fields.setdefault("id", _entry_id(body))
        fields.setdefault("last_seen", legacy_seen_value)
        fields.setdefault("node", node)
        fields.setdefault("scope", "cluster")
        for key, value in _default_fields(kind).items():
            fields.setdefault(key, value)
        output.append(f"{prefix}{body} {format_metadata(fields)}")
    return "\n".join(output).rstrip() + "\n"


def _split_h2_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split ``text`` into ``(heading_line, body_lines)`` per H2 section.

    Lines before the first H2 are returned under an empty heading key ``""`` so
    a preamble (``# Reflections`` + timestamp lines) round-trips. Each section's
    body runs up to (but excluding) the next H2 heading.
    """
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None and len(heading.group(1)) == 2:
            sections.append((current_heading, current_body))
            current_heading = line
            current_body = []
        else:
            current_body.append(line)
    sections.append((current_heading, current_body))
    return sections


def _section_body_without_markers(body_lines: list[str]) -> str:
    """A section body with every ``<!--om-section:`` marker line removed.

    Used as the comparison key for change-detection: two renderings of the same
    section are "unchanged" iff their non-marker bodies are byte-identical, so a
    differing-only provenance stamp never counts as a content change.
    """
    return "\n".join(line for line in body_lines if not _SECTION_META_RE.match(line))


def ensure_section_provenance(
    text: str,
    *,
    obs_window: tuple[str, str] | None,
    now: datetime | None = None,
    prior_text: str = "",
) -> str:
    """Stamp section-level rot-proof provenance onto TOUCHED H2 sections of ``text``.

    For every ``## `` (H2) section whose content CHANGED this run, emit exactly
    one
    ``<!--om-section: last_reflected=<date> derived_from_obs_window=<min>..<max>-->``
    line as the section's first body line, immediately after the heading, and
    drop EVERY pre-existing ``<!--om-section:`` marker anywhere in that section's
    body (self-healing: idempotent, and resilient to a model echoing the stamp
    on a reflowed line rather than heading-adjacent).

    HONEST ``last_reflected``: a section is "changed" iff its non-marker body
    differs from the same-named section in ``prior_text``. An UNCHANGED section
    keeps its prior marker VERBATIM — it was not touched by this reflect, so its
    ``last_reflected`` must not be refreshed. Under the sectioned/auto strategy
    the untouched sections are reassembled byte-for-byte from the prior document,
    so the vast majority of sections at scale correctly retain their honest prior
    stamp. When ``prior_text`` is empty (the default — used by the function's own
    unit tests and any single-pass caller that genuinely rewrote everything),
    every section is treated as changed and restamped, preserving prior behavior.

    ``obs_window`` is the ``(min, max)`` date range of the observation window
    actually folded this run. When it is ``None`` the text is returned
    BYTE-IDENTICAL (strict no-op) — this is the prune/idempotency guarantee: a
    caller with no real reflect window (e.g. ``om prune``) must never fabricate
    or refresh provenance. When the window is present but degenerate
    (``min == max``) a single-day range is stamped (honest, never empty).

    Only H2 headings are stamped; H3 (``### ``) subsection headers are left
    untouched so subsection slug/handle routing is unaffected. The pass is
    fail-closed: any unexpected line is emitted verbatim, and a document with no
    H2 headings returns unchanged.

    NOTE on the two ``last_reflected`` notions: this per-section
    ``last_reflected`` is the WALL-CLOCK UTC date the reflect RAN (``now``),
    answering "when was this section last touched by a reflect". It is
    deliberately distinct from the document-level ``*Last reflected:*`` line
    (stamped to the newest OBSERVATION date by ``_stamp_timestamps``); the two
    can differ by the observe->reflect lag. A synthetic ``## Stale snapshots``
    bucket that ``prune_stale_snapshots`` appends AFTER this pass is intentionally
    left unstamped (it is not derived from an observation window).
    """
    if obs_window is None:
        return text

    now_date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    window_min, window_max = obs_window
    marker = f"<!--om-section: last_reflected={now_date} derived_from_obs_window={window_min}..{window_max}-->"

    # Map prior heading -> non-marker body so an unchanged section is detected by
    # byte-equality of its content (the marker line itself is excluded).
    prior_bodies: dict[str, str] = {}
    for heading_line, body_lines in _split_h2_sections(prior_text):
        if heading_line:
            prior_bodies[heading_line] = _section_body_without_markers(body_lines)

    output: list[str] = []
    for heading_line, body_lines in _split_h2_sections(text):
        if not heading_line:
            # Preamble before the first H2 — emit verbatim, never stamped.
            output.extend(body_lines)
            continue
        current_body = _section_body_without_markers(body_lines)
        prior_body = prior_bodies.get(heading_line)
        unchanged = prior_body is not None and prior_body == current_body
        output.append(heading_line)
        if unchanged:
            # Section was not touched this run: preserve its body verbatim,
            # including any prior `<!--om-section:` marker (do NOT refresh).
            output.extend(body_lines)
        else:
            # Touched (or new) section: emit one fresh marker right after the
            # heading and drop every stale marker from the body (self-healing).
            output.append(marker)
            output.extend(line for line in body_lines if not _SECTION_META_RE.match(line))
    return "\n".join(output).rstrip() + "\n"


def parse_metadata(line: str) -> dict[str, str]:
    match = _META_RE.search(line)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for token in match.group(1).split():
        key, sep, value = token.partition("=")
        if sep and key:
            fields[key] = value
    return fields


def format_metadata(fields: dict[str, str]) -> str:
    ordered = [
        "id",
        "kind",
        "mode",
        "source_type",
        "confidence",
        "sensitivity",
        "actionability",
        "last_seen",
        "last_verified",
        "expires",
        "seen_count",
        "node",
        "scope",
    ]
    parts = []
    for key in ordered:
        if key in fields:
            parts.append(f"{key}={fields[key]}")
    for key in sorted(k for k in fields if k not in ordered):
        parts.append(f"{key}={fields[key]}")
    return "<!--om: " + " ".join(parts) + "-->"


def infer_kind(body: str, section: str = "") -> str:
    lower = f"{section} {body}".lower()
    if "working mode" in lower or "execution mode" in lower or "launch mode" in lower or "mode:" in lower:
        return "mode"
    if "identity" in lower or "name:" in lower or "role:" in lower:
        return "identity"
    if "policy" in lower or "must not" in lower or "never " in lower or "requires explicit approval" in lower:
        return "policy"
    if "preference" in lower or "prefers" in lower or "communication" in lower:
        return "preference"
    snapshot_markers = (
        "pr #",
        "issue #",
        "branch",
        "commit",
        "sha",
        "pending",
        "approved",
        "merged",
        "current",
        "today",
        "yesterday",
    )
    if any(marker in lower for marker in snapshot_markers):
        return "snapshot"
    if "task" in lower or "todo" in lower or "open question" in lower:
        return "task"
    if "decision" in lower or "decided" in lower:
        return "decision"
    return "evergreen"


def filter_reflection_entries_for_cluster(text: str) -> str:
    """Remove host-local reflection entries before writing shared cluster snapshots.

    Drops ``scope=local`` bullet lines. LEAK-CRITICAL (Gate 3): when a section
    reduces to a bare heading with no surviving shared content, its H2 heading AND
    any attached ``<!--om-section:`` provenance stamp are also dropped, so a
    wholly-local section never leaks its title or its reflect cadence / obs-window
    into shared cluster memory. (This also closes the pre-existing orphan-heading
    leak for wholly-local sections.) Sections that retain at least one shared line
    keep their heading and stamp unchanged.
    """
    kept = [line for line in text.splitlines() if parse_metadata(line).get("scope") != "local"]
    output = _drop_empty_heading_sections(kept)
    return "\n".join(output).rstrip() + "\n"


def _line_is_real_content(line: str) -> bool:
    """True if ``line`` is durable shared content — not a heading, blank, or marker.

    Headings (any level) are *structure*, a provenance stamp is *metadata*, and a
    blank line is *whitespace*; none of them keeps a section alive on their own.
    Only a real bullet/prose line counts, so an H2/H3/H4 heading whose body was
    entirely ``scope=local`` is correctly treated as empty.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _SECTION_META_RE.match(line):
        return False
    if _HEADING_RE.match(line):
        return False
    return True


def _drop_empty_heading_sections(lines: list[str]) -> list[str]:
    """Recursively drop any heading block with no real content after filtering.

    Operates at every heading level (H2 through H6), not just H2: a section is
    "empty" when, after local-line filtering, its block (down to the next heading
    of the same-or-shallower level) contains no real content line — where a
    nested sub-heading only counts if *it* survives pruning. Such a block's
    heading and any ``<!--om-section:`` stamp are removed entirely.

    This closes the subsection leak: an H3/H4 whose every bullet was
    ``scope=local`` is dropped along with its title, and an H2 that is left with
    only an empty private subsection is dropped too. Lines before the first
    heading (preamble) and any block with surviving content are kept verbatim.
    """
    if not lines:
        return []
    levels = [len(m.group(1)) for line in lines if (m := _HEADING_RE.match(line))]
    if not levels:
        # No headings here — these are leaf content/blank/marker lines; keep as-is.
        return list(lines)
    top = min(levels)

    output: list[str] = []
    index = 0
    total = len(lines)
    # Preamble: everything before the first top-level heading is kept verbatim.
    while index < total:
        heading = _HEADING_RE.match(lines[index])
        if heading is not None and len(heading.group(1)) == top:
            break
        output.append(lines[index])
        index += 1
    # Each top-level heading + its block (down to the next same-level heading).
    while index < total:
        heading_line = lines[index]
        index += 1
        block: list[str] = []
        while index < total:
            heading = _HEADING_RE.match(lines[index])
            if heading is not None and len(heading.group(1)) == top:
                break
            block.append(lines[index])
            index += 1
        pruned = _drop_empty_heading_sections(block)
        if any(_line_is_real_content(line) for line in pruned):
            output.append(heading_line)
            output.extend(pruned)
        # else: wholly-local / empty block — drop heading + stamp + body entirely.
    return output


def filter_reflection_entries_for_host(text: str, *, local_node: str) -> str:
    """Hide remote host-local entries when materializing generated Markdown."""
    output: list[str] = []
    for line in text.splitlines():
        fields = parse_metadata(line)
        if fields.get("scope") == "local" and fields.get("node") not in {"", local_node}:
            continue
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


def find_reflection_conflicts(snapshots: list[tuple[str, str, str]]) -> list[ReflectionConflict]:
    """Find operator-visible conflicts across reflection snapshot bodies.

    ``snapshots`` contains ``(record_id, node_id, body)`` tuples. This is a
    deterministic heuristic: non-snapshot entries in the same section/kind from
    different records are reviewable when their normalized text differs.
    """
    buckets: dict[tuple[str, str], list[dict[str, str]]] = {}
    for record_id, node_id, body in snapshots:
        section = ""
        for line in body.splitlines():
            heading = _HEADING_RE.match(line)
            if heading:
                section = heading.group(2).strip()
                continue
            bullet = _BULLET_RE.match(line)
            if not bullet:
                continue
            fields = parse_metadata(line)
            kind = fields.get("kind") or infer_kind(bullet.group(2), section)
            actionability = fields.get("actionability", _default_fields(kind)["actionability"])
            if fields.get("scope") == "local" or kind == "snapshot":
                continue
            if kind not in {"identity", "preference", "policy", "decision", "mode"} and actionability != "high":
                continue
            text = bullet.group(2).strip()
            key = (section, kind)
            buckets.setdefault(key, []).append(
                {
                    "record_id": record_id,
                    "node": fields.get("node", node_id),
                    "entry_id": fields.get("id", _entry_id(text)),
                    "actionability": actionability,
                    "text": text,
                }
            )
    conflicts: list[ReflectionConflict] = []
    for (section, kind), entries in sorted(buckets.items()):
        normalized = {entry["text"].casefold() for entry in entries}
        record_ids = {entry["record_id"] for entry in entries}
        if len(normalized) > 1 and len(record_ids) > 1:
            actionability = "high" if any(entry["actionability"] == "high" for entry in entries) else "medium"
            conflicts.append(
                ReflectionConflict(section=section, kind=kind, actionability=actionability, entries=entries)
            )
    return conflicts


def prune_stale_snapshots(
    text: str,
    *,
    now: datetime | None = None,
    ttl_days: int = 14,
    action: str = "stale-section",
) -> tuple[str, PruneSummary]:
    if action not in {"stale-section", "drop", "annotate"}:
        raise ValueError(f"Unsupported snapshot expiry action: {action}")
    now_dt = now or datetime.now(timezone.utc)
    current_section = ""
    output: list[str] = []
    stale: list[str] = []
    summary = PruneSummary()
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current_section = heading.group(2).strip()
            output.append(line)
            continue
        fields = parse_metadata(line)
        if fields.get("kind") != "snapshot" or current_section.lower() == "stale snapshots":
            output.append(line)
            continue
        if not _is_stale(fields.get("last_seen"), now_dt, ttl_days):
            output.append(line)
            continue
        if action == "drop":
            summary = PruneSummary(
                pruned=summary.pruned + 1,
                annotated=summary.annotated,
                stale_sectioned=summary.stale_sectioned,
            )
            continue
        if action == "annotate":
            if "stale=true" not in line:
                line = line.rstrip() + " <!-- stale=true -->"
            output.append(line)
            summary = PruneSummary(
                pruned=summary.pruned,
                annotated=summary.annotated + 1,
                stale_sectioned=summary.stale_sectioned,
            )
            continue
        stale.append(line)
        summary = PruneSummary(
            pruned=summary.pruned,
            annotated=summary.annotated,
            stale_sectioned=summary.stale_sectioned + 1,
        )
    if stale:
        while output and output[-1] == "":
            output.pop()
        output.extend(["", "## Stale snapshots", "", *stale])
    return "\n".join(output).rstrip() + "\n", summary


def _is_stale(value: str | None, now: datetime, ttl_days: int) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).days >= ttl_days


def _entry_id(body: str) -> str:
    return "ome_" + sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_fields(kind: str) -> dict[str, str]:
    actionability = {
        "identity": "high",
        "policy": "high",
        "preference": "medium",
        "decision": "medium",
        "task": "medium",
        "mode": "high",
        "snapshot": "low",
        "evergreen": "medium",
    }.get(kind, "medium")
    sensitivity = "normal"
    if kind in {"identity", "policy"}:
        sensitivity = "personal"
    return {
        "source_type": "inferred",
        "confidence": "medium",
        "sensitivity": sensitivity,
        "actionability": actionability,
        "seen_count": "1",
    }
