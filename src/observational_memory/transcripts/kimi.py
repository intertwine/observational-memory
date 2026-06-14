"""Parse Observational Memory hook events captured from Kimi Code CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import Message


def parse_transcript(path: Path, *, after_index: int = 0) -> list[Message]:
    """Parse OM's Kimi hook-event log into normalized messages."""
    messages: list[Message] = []
    if not path.exists():
        return messages

    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if line_no <= after_index or not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = _event_to_message(event)
        if msg is not None:
            messages.append(msg)
    return messages


def _event_to_message(event: dict[str, Any]) -> Message | None:
    name = str(event.get("hook_event_name") or event.get("event") or "")
    timestamp = str(event.get("om_captured_at") or _now())

    if name == "UserPromptSubmit":
        content = str(event.get("prompt") or "").strip()
        if not content:
            return None
        return Message(role="user", content=content, timestamp=timestamp, source="kimi")

    if name == "SubagentStart":
        prompt = str(event.get("prompt") or "").strip()
        agent = str(event.get("agent_name") or "subagent").strip() or "subagent"
        if not prompt:
            return None
        return Message(role="user", content=f"[{agent} subagent prompt] {prompt}", timestamp=timestamp, source="kimi")

    if name == "SubagentStop":
        response = str(event.get("response") or "").strip()
        agent = str(event.get("agent_name") or "subagent").strip() or "subagent"
        if not response:
            return None
        return Message(
            role="assistant",
            content=f"[{agent} subagent response] {response}",
            timestamp=timestamp,
            source="kimi",
        )

    if name == "StopFailure":
        error = str(event.get("error_message") or event.get("error_type") or "").strip()
        if not error:
            return None
        return Message(role="assistant", content=f"Kimi turn failed: {error}", timestamp=timestamp, source="kimi")

    return None


def count_events(path: Path) -> int:
    """Return raw line count for cursor advancement."""
    if not path.exists():
        return 0
    return len(path.read_text().splitlines())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
