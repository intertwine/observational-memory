"""Parse Claude Code transcript JSONL files into normalized messages."""

from __future__ import annotations

import json
from pathlib import Path

from . import Message


def parse_transcript(path: Path, after_uuid: str | None = None) -> list[Message]:
    """Parse a Claude Code .jsonl transcript into Messages.

    Args:
        path: Path to the transcript .jsonl file.
        after_uuid: If set, only return messages after this UUID (for incremental processing).

    Returns:
        List of normalized Message objects.
    """
    messages: list[Message] = []
    seen_after = after_uuid is None  # if no cursor, include everything

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip non-message entries
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue

        uuid = entry.get("uuid", "")
        timestamp = entry.get("timestamp", "")
        msg = entry.get("message", {})
        role = msg.get("role", "")

        # Skip until we pass the cursor
        if not seen_after:
            if uuid == after_uuid:
                seen_after = True
            continue

        # Skip meta messages (like skill expansions)
        if entry.get("isMeta"):
            continue

        content = _extract_content(msg)
        if content:
            messages.append(Message(
                role=role,
                content=content,
                timestamp=timestamp,
                source="claude",
            ))

    return messages


def _extract_content(msg: dict) -> str:
    """Extract readable text from a Claude Code message, summarizing tool use."""
    content = msg.get("content", "")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool = block.get("name", "unknown")
                    inp = block.get("input", {})
                    parts.append(_summarize_tool_use(tool, inp))
                elif block_type == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, str) and len(result) < 500:
                        parts.append(f"[result: {result[:200]}]")
        return "\n".join(p for p in parts if p).strip()

    return ""


def _summarize_tool_use(tool: str, inp: dict) -> str:
    """Create a one-line summary of a tool call."""
    if tool == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return f"[Bash: {desc or cmd[:100]}]"
    elif tool == "Read":
        return f"[Read: {inp.get('file_path', '?')}]"
    elif tool in ("Write", "Edit"):
        return f"[{tool}: {inp.get('file_path', '?')}]"
    elif tool == "Glob":
        return f"[Glob: {inp.get('pattern', '?')}]"
    elif tool == "Grep":
        return f"[Grep: {inp.get('pattern', '?')}]"
    elif tool == "WebSearch":
        return f"[WebSearch: {inp.get('query', '?')}]"
    elif tool == "WebFetch":
        return f"[WebFetch: {inp.get('url', '?')}]"
    elif tool == "Task":
        return f"[Task: {inp.get('description', '?')}]"
    else:
        return f"[{tool}]"


def find_recent_transcripts(projects_dir: Path, max_age_hours: int = 24) -> list[Path]:
    """Find Claude Code transcript files modified within max_age_hours."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    transcripts = []

    if not projects_dir.exists():
        return transcripts

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
            if mtime > cutoff:
                transcripts.append(jsonl)

    return sorted(transcripts, key=lambda p: p.stat().st_mtime, reverse=True)


def find_all_transcripts(projects_dir: Path) -> list[Path]:
    """Find ALL Claude Code transcript files, sorted oldest-first by modification time."""
    transcripts = []
    if not projects_dir.exists():
        return transcripts
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            transcripts.append(jsonl)
    return sorted(transcripts, key=lambda p: p.stat().st_mtime)
