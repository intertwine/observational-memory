"""Parse Codex CLI session transcripts into normalized messages."""

from __future__ import annotations

import json
from pathlib import Path

from . import Message


def parse_transcript(path: Path, after_index: int | None = None) -> list[Message]:
    """Parse a Codex session transcript into Messages.

    Codex stores sessions as JSONL in ~/.codex/sessions/. The exact format
    may vary by version; this parser handles the common structure where each
    line has type, role, content, and timestamp fields.

    Args:
        path: Path to the session file.
        after_index: If set, skip the first N entries (for incremental processing).

    Returns:
        List of normalized Message objects.
    """
    messages: list[Message] = []

    lines = path.read_text().splitlines()
    start = (after_index or 0)

    for line in lines[start:]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
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

    for f in sessions_dir.rglob("*.jsonl"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            sessions.append(f)

    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)
