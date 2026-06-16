"""Transcript parsers for Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Grok Build TUI, and Hermes Agent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Message:
    """Normalized message from any agent transcript."""

    role: str  # "user" or "assistant"
    content: str  # text content (tool calls summarized)
    timestamp: str  # ISO 8601
    source: str  # "claude", "codex", "opencode", "kimi", "grok", "cowork", or "hermes"
