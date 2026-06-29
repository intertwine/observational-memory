"""Parse Aside browser-agent session transcripts into normalized messages.

Aside (the agentic browser by the Cowork/Claude team's ecosystem) persists one
JSONL transcript per session at::

    ~/.aside/u/<user-index>/agents/<agent>/sessions/<date>_<session-id>/messages.jsonl

Unlike Claude Code / Cowork ``audit.jsonl`` (which key each record by ``type``
and ``uuid``), Aside records are keyed by ``role`` and carry no per-message
UUID, so resumption is **count-based** (like Codex and Grok) rather than
cursor-by-uuid.

Record shape (one JSON object per line)::

    {"role": "user", "content": "...", "timestamp": 1782425628579}
    {"role": "assistant", "content": [<block>, ...], "timestamp": 1782425630000,
     "provider": "anthropic", "model": "...", "responseId": "...", ...}
    {"role": "toolResult", "toolName": "read_file", "content": [...], ...}
    {"role": "system-message", "content": "...", "kind": "site_skill", ...}

Assistant ``content`` is a list of typed blocks::

    {"type": "text", "text": "..."}
    {"type": "thinking", "thinking": "...", "thinkingSignature": "..."}
    {"type": "toolCall", "id": "...", "name": "read_file", "arguments": {...}}

Only ``user`` and ``assistant`` records produce observations. ``toolResult`` and
``system-message`` records are skipped — the ``toolCall`` summary inside the
assistant turn already carries the action signal (the same philosophy as the
Claude Code parser). ``thinking`` blocks are dropped as internal reasoning.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import Message

# Per-user Aside home. Override with ASIDE_HOME for tests / non-default installs.
ASIDE_HOME = Path.home() / ".aside"

# Glob (relative to the Aside home) that matches every session transcript across
# all local user indices and agents.
_SESSION_GLOB = "u/*/agents/*/sessions/*/messages.jsonl"


def _to_iso(timestamp: object) -> str:
    """Normalize an Aside timestamp (epoch milliseconds) to ISO 8601 UTC."""
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    if isinstance(timestamp, str):
        return timestamp
    return ""


def _summarize_tool_call(name: str, arguments: object) -> str:
    """Create a one-line summary of an Aside tool call.

    Tool names are matched against Aside's real (lowercase) tool registry so
    observations read consistently across harnesses, mirroring the Claude Code
    parser's ``_summarize_tool_use``.
    """
    args = arguments if isinstance(arguments, dict) else {}
    key = (name or "tool").lower()
    if key == "bash":
        return f"[bash: {args.get('title') or str(args.get('command', ''))[:100]}]"
    if key in ("read_file", "read"):
        return f"[read_file: {args.get('path') or args.get('file_path', '?')}]"
    if key in ("write_file", "edit_file"):
        return f"[{key}: {args.get('path') or args.get('file_path', '?')}]"
    if key == "repl":
        return f"[repl: {args.get('title', 'run code')}]"
    if key == "websearch":
        return f"[websearch: {args.get('objective') or args.get('query', '?')}]"
    if key == "webfetch":
        return f"[webfetch: {args.get('url', '?')}]"
    if key == "subagent":
        return f"[subagent: {args.get('description') or args.get('action', '?')}]"
    if key in ("memory_search", "browsing_history_search"):
        return f"[{key}: {args.get('queries', '?')}]"
    if key == "write_todos":
        return "[write_todos]"
    return f"[{name}]"


def _extract_content(content: object) -> str:
    """Extract readable text from an Aside record, summarizing tool calls.

    Accepts either a plain string (user turns) or a list of typed blocks
    (assistant turns). ``thinking`` blocks are dropped.
    """
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "toolCall":
                    parts.append(_summarize_tool_call(block.get("name", "tool"), block.get("arguments")))
                # "thinking" and any unknown block types are intentionally skipped.
        return "\n".join(p for p in parts if p).strip()

    return ""


def parse_transcript(path: Path, source: str = "aside") -> list[Message]:
    """Parse an Aside ``messages.jsonl`` into normalized Messages.

    Returns **all** user/assistant messages. Incremental resumption is handled by
    the caller via a count-based cursor (see ``observe.observe_aside_transcript``),
    matching the Codex/Grok convention for UUID-less transcripts.
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

        role = entry.get("role")
        if role not in ("user", "assistant"):
            # Skip system-message and toolResult records.
            continue

        content = _extract_content(entry.get("content"))
        if not content:
            continue

        messages.append(
            Message(
                role=role,
                content=content,
                timestamp=_to_iso(entry.get("timestamp", "")),
                source=source,
            )
        )

    return messages


def find_recent_transcripts(aside_home: Path | None = None, max_age_hours: int = 24) -> list[Path]:
    """Find Aside ``messages.jsonl`` files modified within *max_age_hours*.

    Returns paths sorted newest-first (matching the other harness parsers).
    """
    base = aside_home or ASIDE_HOME
    if not base.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    transcripts: list[Path] = []
    for transcript in base.glob(_SESSION_GLOB):
        mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            transcripts.append(transcript)

    return sorted(transcripts, key=lambda p: p.stat().st_mtime, reverse=True)


def find_all_transcripts(aside_home: Path | None = None) -> list[Path]:
    """Find ALL Aside ``messages.jsonl`` files, sorted oldest-first."""
    base = aside_home or ASIDE_HOME
    if not base.exists():
        return []
    return sorted(base.glob(_SESSION_GLOB), key=lambda p: p.stat().st_mtime)
