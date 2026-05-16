"""Parse Grok Build TUI session updates.jsonl into normalized messages.

Grok stores rich session events in JSONL format under
~/.grok/sessions/<cwd-encoded>/<session-id>/updates.jsonl.

Event shape (example):
{
  "timestamp": 1778885590,
  "method": "session/update",
  "params": {
    "sessionId": "...",
    "update": {
      "sessionUpdate": "user_message" | "assistant_message" | ...,
      "content": "..."
    }
  }
}

This parser is intentionally defensive for the early-beta nature of Grok.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import Message


def find_recent_grok_sessions(sessions_dir: Path | None = None) -> list[Path]:
    """Discover recent Grok session transcripts (updates.jsonl files).

    Sessions are under ~/.grok/sessions/<cwd-encoded>/<session-id>/updates.jsonl
    """
    if sessions_dir is None:
        sessions_dir = Path.home() / ".grok" / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(
        sessions_dir.glob("*/*/updates.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:50]  # Limit to recent ones, similar to Codex pattern


def parse_transcript(
    path: Path, after_uuid: str | None = None, source: str = "grok", after_index: int | None = None
) -> list[Message]:
    """Parse a Grok updates.jsonl into normalized Messages.

    Supports count-based resumption via after_index (preferred for Grok to avoid
    same-second chunk reprocessing) or legacy timestamp-based via after_uuid.
    """
    messages: list[Message] = []
    seen_after = after_uuid is None and after_index is None
    message_count = 0

    if not path.exists():
        return messages

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("method") != "session/update":
            continue

        update = entry.get("params", {}).get("update", {})
        update_type = update.get("sessionUpdate", "")

        content = _extract_grok_content(update)
        if not content:
            continue

        message_count += 1

        if not seen_after:
            if after_index is not None:
                if message_count <= after_index:
                    continue
                seen_after = True
            elif after_uuid:
                ts = str(entry.get("timestamp", ""))
                if ts == after_uuid:
                    seen_after = True
                continue
            else:
                seen_after = True

        if "user_message" in update_type or "user_message_chunk" in update_type:
            role = "user"
        else:
            role = "assistant"
        messages.append(
            Message(
                role=role,
                content=content,
                timestamp=str(entry.get("timestamp", "")),
                source=source,
            )
        )

    return messages


def _extract_grok_content(update: dict[str, Any]) -> str:
    """Extract human-readable text from a Grok session update.

    Handles real event types observed on this machine:
    user_message_chunk, agent_message_chunk, agent_thought_chunk,
    tool_call, tool_call_update, etc.
    Content is often {"type": "text", "text": "..."} or plain string.
    """
    utype = update.get("sessionUpdate", "")

    content = update.get("content")

    # Handle structured content {"type": "text", "text": "..."}
    if isinstance(content, dict):
        if content.get("type") == "text":
            text = content.get("text", "")
            if isinstance(text, str) and text.strip():
                # Clean up embedded tool call XML for readability in observations
                if "<tool_call>" in text:
                    text = text.split("<tool_call>")[0].strip() + " [tool call details omitted for observation]"
                return text.strip()
        # Fallback to any string value in the dict
        for v in content.values():
            if isinstance(v, str) and v.strip():
                return v.strip()

    if isinstance(content, str) and content.strip():
        return content.strip()

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(block["text"])
                elif "text" in block:
                    parts.append(block["text"])
        joined = "\n".join(p for p in parts if p).strip()
        if joined:
            return joined

    # Tool events
    if "tool_call" in utype or "tool_call_update" in utype:
        name = update.get("name") or update.get("tool", {}).get("name", "unknown_tool")
        return f"[Grok tool call: {name}]"

    # Thought chunks often contain partial reasoning + tool XML
    if "thought" in utype or "agent_thought" in utype:
        # Already handled above if content had text; fallback
        for key in ("text", "delta", "content"):
            val = update.get(key)
            if isinstance(val, str) and val.strip():
                return "[thought] " + val.strip()[:500]

    # Generic fallback for any text-like field
    for key in ("text", "delta", "message", "content"):
        val = update.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict) and "text" in val:
            return str(val["text"]).strip()

    return ""
