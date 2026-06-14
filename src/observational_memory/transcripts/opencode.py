"""Parse Observational Memory's OpenCode plugin event log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import Message


def find_recent_sessions(events_dir: Path) -> list[Path]:
    """Find recent OpenCode plugin JSONL transcripts."""
    if not events_dir.exists():
        return []
    return sorted(events_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]


def parse_transcript(path: Path, source: str = "opencode") -> list[Message]:
    """Parse OpenCode plugin JSONL events into normalized messages.

    The plugin forwards raw OpenCode events to `om opencode-event`. OpenCode's
    event payloads can change, so this parser accepts several common message
    shapes and ignores non-message lifecycle events.
    """
    messages: list[Message] = []
    if not path.exists():
        return messages

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = _extract_message(entry)
        if msg is None:
            continue
        role, content, timestamp = msg
        messages.append(Message(role=role, content=content, timestamp=timestamp, source=source))
    return messages


def _extract_message(entry: dict[str, Any]) -> tuple[str, str, str] | None:
    event = entry.get("event") if isinstance(entry.get("event"), dict) else entry
    if not isinstance(event, dict):
        return None

    event_type = str(event.get("type") or entry.get("type") or "")
    if event_type and "message" not in event_type and "part" not in event_type:
        return None

    candidate = event.get("message") if isinstance(event.get("message"), dict) else event
    role = _normalize_role(candidate.get("role") or candidate.get("author") or candidate.get("speaker"))
    content = _extract_text(candidate)

    if not content and isinstance(event.get("part"), dict):
        part = event["part"]
        role = role or _normalize_role(part.get("role"))
        content = _extract_text(part)

    if not role or not content:
        return None

    timestamp = str(
        candidate.get("time")
        or candidate.get("timestamp")
        or candidate.get("created")
        or event.get("time")
        or event.get("timestamp")
        or entry.get("received_at")
        or ""
    )
    return role, content, timestamp


def _normalize_role(value: object) -> str | None:
    role = str(value or "").lower()
    if role in {"user", "assistant", "system"}:
        return role
    if role in {"agent", "ai", "model"}:
        return "assistant"
    return None


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""

    for key in ("text", "content", "message"):
        raw = value.get(key)
        text = _text_from_content(raw)
        if text:
            return text

    parts = value.get("parts") or value.get("content")
    return _text_from_content(parts)


def _text_from_content(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(part for part in parts if part).strip()
    if isinstance(raw, dict):
        if isinstance(raw.get("text"), str):
            return raw["text"].strip()
        if isinstance(raw.get("content"), str):
            return raw["content"].strip()
    return ""
