"""Parse Codex CLI session transcripts into normalized messages."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import Message

_LOGGER = logging.getLogger(__name__)


def _coerce_records(value: Any) -> list[dict]:
    """Normalize parsed JSON payloads to a list of dict records."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [record for record in value if isinstance(record, dict)]
    return []


def _extract_records(raw: str, source_path: Path) -> list[dict]:
    """Extract message-like dicts from either full JSON sessions or JSONL sessions."""
    records: list[dict] = []

    # Try full JSON document first (Codex sessions are sometimes pretty-printed JSON objects).
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("Failed to parse Codex transcript %s as JSON: %s", source_path, exc)
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
            except json.JSONDecodeError as exc:
                _LOGGER.warning(
                    "Skipping malformed JSON line in Codex transcript %s: %s",
                    source_path,
                    exc,
                )
                continue
            if isinstance(entry, list):
                records.extend(_coerce_records(entry))
            else:
                records.extend(_coerce_records(entry))

    return records


def line_offset_to_message_count(path: Path, line_offset: int) -> int:
    """Translate a legacy raw line cursor into a message index.

    Historically, Codex cursors were tracked by transcript line offsets. New
    parsing is message-based, so older cursor values can overrun the parsed list.
    """
    if line_offset <= 0:
        return 0

    if path.suffix.lower() != ".jsonl":
        return 0

    try:
        lines = path.read_text().splitlines()[:line_offset]
    except OSError as exc:
        _LOGGER.warning("Failed to read Codex session %s for cursor migration: %s", path, exc)
        return 0

    message_count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            _LOGGER.warning(
                "Skipping malformed JSON line while migrating Codex cursor for %s: %s",
                path,
                exc,
            )
            continue

        for item in _coerce_records(entry):
            role = item.get("role", "")
            if role not in ("user", "assistant"):
                nested_message = item.get("message")
                if isinstance(nested_message, dict):
                    role = nested_message.get("role", "")
            if role in ("user", "assistant"):
                message_count += 1

    return message_count


def parse_transcript(path: Path, after_index: int | None = None) -> list[Message]:
    """Parse a Codex session transcript into Messages.

    Codex sessions can be JSONL or full JSON documents. The parser handles both
    formats and normalizes common message structures.

    Args:
    path: Path to the session file.
    after_index: If set, skip the first N parsed entries (for incremental processing).

    Returns:
        List of normalized Message objects.
    """
    messages: list[Message] = []
    start = max(after_index or 0, 0)

    records = _extract_records(path.read_text(), path)

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
