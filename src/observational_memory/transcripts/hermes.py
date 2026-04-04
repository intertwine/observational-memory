"""Parse Hermes Agent session JSONL files into normalized messages.

Hermes v0.7.0 session logs are JSONL with one JSON object per line.
Records include: session_meta, user, assistant (with optional tool_calls),
and tool result messages. This parser extracts only the human-meaningful
conversation — user input and assistant prose — while summarizing tool
calls as compact one-liners and discarding raw tool output, session_meta,
and machine-oriented records.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import Message

_LOGGER = logging.getLogger(__name__)


def parse_transcript(
    path: Path,
    after_index: int | None = None,
) -> list[Message]:
    """Parse a Hermes session JSONL file into normalized Messages.

    Filters to user and assistant messages only. Tool calls in assistant
    messages are summarized as one-liners. Tool result messages and
    session_meta records are discarded entirely.

    Args:
        path: Path to the session .jsonl file.
        after_index: If set, skip the first N parsed messages (for
            incremental processing).

    Returns:
        List of normalized Message objects.
    """
    messages: list[Message] = []
    start = max(after_index or 0, 0)

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(entry, dict):
            continue

        role = entry.get("role", "")

        # Only keep user and assistant messages
        if role not in ("user", "assistant"):
            continue

        timestamp = entry.get("timestamp", "")
        content = _extract_content(entry)

        if content:
            messages.append(
                Message(
                    role=role,
                    content=content,
                    timestamp=timestamp,
                    source="hermes",
                )
            )

    return messages[start:]


def _extract_content(entry: dict) -> str:
    """Extract readable text from a Hermes message, summarizing tool calls.

    For assistant messages with tool_calls, appends compact summaries.
    For messages where finish_reason is 'tool_calls' and there's no
    prose content, returns only the tool summaries.
    """
    role = entry.get("role", "")
    content = entry.get("content", "")

    # User messages: just return content
    if role == "user":
        if isinstance(content, str):
            return content.strip()
        return ""

    # Assistant messages: extract prose + summarize tool calls
    parts = []

    # Extract prose content
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                parts.append(block.strip())
            elif isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text:
                        parts.append(text)

    # Summarize tool calls (if present)
    tool_calls = entry.get("tool_calls", [])
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if isinstance(func, dict):
                name = func.get("name", "unknown")
                args = func.get("arguments", "")
                parts.append(_summarize_tool_call(name, args))

    # Skip assistant messages that are pure tool-call stubs with no prose
    # These are machine-oriented and add no observational value
    has_prose = any(p and not p.startswith("[") for p in parts)
    if not has_prose and entry.get("finish_reason") == "tool_calls":
        return ""

    return "\n".join(p for p in parts if p).strip()


def _summarize_tool_call(name: str, raw_args: str) -> str:
    """Create a compact one-line summary of a Hermes tool call."""
    args = {}
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            pass
    elif isinstance(raw_args, dict):
        args = raw_args

    if name == "terminal":
        cmd = args.get("command", "")
        return f"[terminal: {cmd[:100]}]" if cmd else f"[{name}]"
    elif name in ("file_read", "read_file"):
        return f"[read: {args.get('path', args.get('file_path', '?'))}]"
    elif name in ("file_write", "write_file", "file_edit"):
        return f"[{name}: {args.get('path', args.get('file_path', '?'))}]"
    elif name == "web_search":
        return f"[web_search: {args.get('query', '?')}]"
    elif name == "web_fetch":
        return f"[web_fetch: {args.get('url', '?')[:80]}]"
    elif name in ("recall", "session_search"):
        return f"[recall: {args.get('query', '?')}]"
    elif name == "delegate":
        return f"[delegate: {args.get('task', '?')[:80]}]"
    elif name == "skill":
        return f"[skill: {args.get('name', '?')}]"
    elif name == "cron":
        action = args.get("action", "")
        return f"[cron: {action}]" if action else f"[{name}]"
    else:
        return f"[{name}]"


def find_recent_sessions(
    sessions_dir: Path,
    max_age_hours: int = 24,
) -> list[Path]:
    """Find Hermes session files modified within max_age_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    sessions = []

    if not sessions_dir.exists():
        return sessions

    for f in sessions_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() != ".jsonl":
            continue
        # Skip index/metadata files
        if f.name in ("sessions.json",):
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            sessions.append(f)

    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def find_all_sessions(sessions_dir: Path) -> list[Path]:
    """Find ALL Hermes session JSONL files, sorted oldest-first."""
    if not sessions_dir.exists():
        return []

    sessions = [
        f for f in sessions_dir.iterdir() if f.is_file() and f.suffix.lower() == ".jsonl" and f.name != "sessions.json"
    ]
    return sorted(sessions, key=lambda p: p.stat().st_mtime)
