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


def ensure_reflection_metadata(text: str, *, now: datetime | None = None, node: str = "local") -> str:
    now_value = _iso(now or datetime.now(timezone.utc))
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
        fields.setdefault("id", _entry_id(body))
        fields.setdefault("kind", infer_kind(body, current_section))
        fields.setdefault("last_seen", now_value)
        fields.setdefault("node", node)
        fields.setdefault("scope", "cluster")
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
    ordered = ["id", "kind", "last_seen", "node", "scope"]
    parts = []
    for key in ordered:
        if key in fields:
            parts.append(f"{key}={fields[key]}")
    for key in sorted(k for k in fields if k not in ordered):
        parts.append(f"{key}={fields[key]}")
    return "<!--om: " + " ".join(parts) + "-->"


def infer_kind(body: str, section: str = "") -> str:
    lower = f"{section} {body}".lower()
    if "identity" in lower or "name:" in lower or "role:" in lower:
        return "identity"
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
