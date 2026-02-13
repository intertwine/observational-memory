"""Parse Codex CLI session transcripts into normalized messages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import Message


def _coerce_records(value: Any) -> list[dict]:
    """Normalize parsed JSON payloads to a list of dict records."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [record for record in value if isinstance(record, dict)]
    return []


def _extract_records(raw: str) -> list[dict]:
    """Extract message-like dicts from either full JSON sessions or JSONL sessions."""
    records: list[dict] = []

    # Try full JSON document first (Codex sessions are sometimes pretty-printed JSON objects).
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    else:
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                records.extend(_coerce_records(items))
            else:
                records.extend(_coerce_records(payload))
        else:
            records.extend(_coerce_records(payload))

    # Fall back to JSONL line parsing (legacy format).
    if not records:
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, list):
                records.extend(_coerce_records(entry))
            else:
                records.extend(_coerce_records(entry))

    return records


def parse_transcript(path: Path, after_index: int | None = None) -> list[Message]:
    """Parse a Codex session transcript into Messages.

    Codex stores sessions as JSONL in ~/.codex/sessions/. The exact format
    may vary by version; this parser handles the common structure where each
    line has type, role, content, and timestamp fields.

    Args:
    path: Path to the session file.
    after_index: If set, skip the first N parsed entries (for incremental processing).

    Returns:
        List of normalized Message objects.
    """
    messages: list[Message] = []
    start = max(after_index or 0, 0)

    records = _extract_records(path.read_text())

    for entry in records[start:]:
        if not isinstance(entry, dict):
            continue

        role = entry.get("role", "")
        if role not in ("user", "assistant"):
            # Try nested message structure
            msg = entry.get("message", {})
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = _extract_content(msg)
            timestamp = entry.get("timestamp", entry.get("created_at", ""))
        else:
            content = _extract_content(entry)
            timestamp = entry.get("timestamp", entry.get("created_at", ""))

        if content:
            messages.append(Message(
                role=role,
                content=content,
                timestamp=timestamp,
                source="codex",
            ))

    return messages


def _extract_content(entry: dict) -> str:
    """Extract text content from a Codex message entry."""
    content = entry.get("content", "")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    parts.append(text)
                # Summarize tool calls
                tool_type = block.get("type", "")
                if tool_type in ("tool_use", "function_call"):
                    name = block.get("name", block.get("function", {}).get("name", "?"))
                    parts.append(f"[Tool: {name}]")
                elif tool_type == "tool_result":
                    result = block.get("output", block.get("content", ""))
                    if isinstance(result, str) and len(result) < 300:
                        parts.append(f"[result: {result[:200]}]")
        return "\n".join(p for p in parts if p).strip()

    return ""


def find_recent_sessions(codex_home: Path, max_age_hours: int = 24) -> list[Path]:
    """Find Codex session files modified within max_age_hours."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sessions = []

    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return sessions

    for f in sessions_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in {".json", ".jsonl"}:
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            sessions.append(f)

    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)
