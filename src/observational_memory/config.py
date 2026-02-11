"""Paths, defaults, and environment detection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


ENV_FILE_TEMPLATE = """\
# Observational Memory — API Keys
# This file is sourced by om, its hooks, and its cron jobs.
# It is NOT committed to any repo. Keep it private.
#
# Uncomment and set exactly one (or both — Anthropic is preferred when both exist):

# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...

# Search backend: bm25 (default), qmd, qmd-hybrid, none
# OM_SEARCH_BACKEND=bm25
"""


@dataclass
class Config:
    """Runtime configuration — resolved from env vars and defaults."""

    # Memory storage
    memory_dir: Path = field(default_factory=lambda: _xdg_data_home() / "observational-memory")

    # Env file for API keys
    env_file: Path = field(default_factory=lambda: _xdg_config_home() / "observational-memory" / "env")

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

    # Search settings
    search_backend: str = field(
        default_factory=lambda: os.environ.get("OM_SEARCH_BACKEND", "bm25")
    )  # "bm25" | "qmd" | "qmd-hybrid" | "none"

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
    def search_index_dir(self) -> Path:
        return self.memory_dir / ".search-index"

    @property
    def codex_agents_md(self) -> Path:
        return self.codex_home / "AGENTS.md"

    def ensure_memory_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def load_env_file(self) -> None:
        """Load API keys from the env file into os.environ (if not already set)."""
        if not self.env_file.exists():
            return
        for line in self.env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            # Don't overwrite keys already in the environment
            if key and key not in os.environ:
                os.environ[key] = value

    def ensure_env_file(self) -> bool:
        """Create the env file from template if it doesn't exist. Returns True if created."""
        if self.env_file.exists():
            return False
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text(ENV_FILE_TEMPLATE)
        self.env_file.chmod(0o600)
        return True

    def detect_provider(self) -> str:
        """Auto-detect which LLM API to use based on available keys."""
        if self.llm_provider:
            return self.llm_provider
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        raise RuntimeError(
            "No LLM API key found. Add your key to "
            f"{self.env_file} or set ANTHROPIC_API_KEY / OPENAI_API_KEY."
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
