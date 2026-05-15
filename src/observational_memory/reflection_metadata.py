"""Inline metadata helpers for reflection entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

_META_RE = re.compile(r"\s*<!--om:\s*(.*?)\s*-->\s*$")
_BULLET_RE = re.compile(r"^(\s*[-*]\s+)(.*?)(\s*<!--om:\s*.*?\s*-->\s*)?$")
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")


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
    """Remove host-local reflection entries before writing shared cluster snapshots."""
    output: list[str] = []
    for line in text.splitlines():
        fields = parse_metadata(line)
        if fields.get("scope") == "local":
            continue
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


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
