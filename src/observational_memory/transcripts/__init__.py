"""Transcript parsers for Claude Code, Codex CLI, Grok Build TUI, Hermes Agent, and Aside."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Message:
    """Normalized message from any agent transcript."""

    role: str  # "user" or "assistant"
    content: str  # text content (tool calls summarized)
    timestamp: str  # ISO 8601
    source: str  # "claude", "codex", "grok", "hermes", "cowork", or "aside"
