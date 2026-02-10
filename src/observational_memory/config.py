"""Paths, defaults, and environment detection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


@dataclass
class Config:
    """Runtime configuration — resolved from env vars and defaults."""

    # Memory storage
    memory_dir: Path = field(default_factory=lambda: _xdg_data_home() / "observational-memory")

    # Claude Code paths
    claude_projects_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    claude_settings_path: Path = field(default_factory=lambda: Path.home() / ".claude" / "settings.json")

    # Codex CLI paths
    codex_home: Path = field(default_factory=lambda: Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))

    # LLM settings
    llm_provider: str | None = None  # "anthropic" | "openai" | None (auto-detect)
    anthropic_model: str = "claude-sonnet-4-5-20250929"
    openai_model: str = "gpt-4o-mini"

    # Observer settings
    min_messages: int = 5  # skip if fewer new messages

    # Reflector settings
    observation_retention_days: int = 7
    reflections_target_lines: int = 400  # aim for 200-600

    @property
    def observations_path(self) -> Path:
        return self.memory_dir / "observations.md"

    @property
    def reflections_path(self) -> Path:
        return self.memory_dir / "reflections.md"

    @property
    def cursor_path(self) -> Path:
        return self.memory_dir / ".cursor.json"

    @property
    def codex_agents_md(self) -> Path:
        return self.codex_home / "AGENTS.md"

    def ensure_memory_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def detect_provider(self) -> str:
        """Auto-detect which LLM API to use based on available keys."""
        if self.llm_provider:
            return self.llm_provider
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        raise RuntimeError(
            "No LLM API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
        )

    # --- Cursor (bookmark) management ---

    def load_cursor(self) -> dict:
        """Load the bookmark file tracking last-processed positions."""
        if self.cursor_path.exists():
            return json.loads(self.cursor_path.read_text())
        return {}

    def save_cursor(self, cursor: dict) -> None:
        self.ensure_memory_dir()
        self.cursor_path.write_text(json.dumps(cursor, indent=2) + "\n")
