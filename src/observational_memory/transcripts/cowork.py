"""Discover Cowork (local-agent-mode) session transcripts.

Cowork sessions live under:
    ~/Library/Application Support/Claude/local-agent-mode-sessions/<org>/<user>/local_<session>/audit.jsonl

The audit.jsonl format is identical to Claude Code JSONL, so parsing is
delegated to :func:`claude.parse_transcript` with ``source="cowork"``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default base directory for Cowork sessions.
COWORK_SESSIONS_DIR = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"


def find_recent_transcripts(sessions_dir: Path | None = None, max_age_hours: int = 24) -> list[Path]:
    """Find Cowork audit.jsonl files modified within *max_age_hours*.

    Returns paths sorted newest-first (matching the Claude Code convention).
    """
    base = sessions_dir or COWORK_SESSIONS_DIR
    if not base.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    transcripts: list[Path] = []

    for audit in base.glob("*/*/local_*/audit.jsonl"):
        mtime = datetime.fromtimestamp(audit.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            transcripts.append(audit)

    return sorted(transcripts, key=lambda p: p.stat().st_mtime, reverse=True)


def find_all_transcripts(sessions_dir: Path | None = None) -> list[Path]:
    """Find ALL Cowork audit.jsonl files, sorted oldest-first."""
    base = sessions_dir or COWORK_SESSIONS_DIR
    if not base.exists():
        return []

    transcripts = list(base.glob("*/*/local_*/audit.jsonl"))
    return sorted(transcripts, key=lambda p: p.stat().st_mtime)
