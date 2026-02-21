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
# LLM provider selection:
# OM_LLM_PROVIDER=auto  # auto|anthropic|openai|anthropic-vertex|anthropic-bedrock
#
# Shared/default model for observer + reflector:
# OM_LLM_MODEL=claude-sonnet-4-5-20250929
#
# Optional per-step model overrides:
# OM_LLM_OBSERVER_MODEL=claude-sonnet-4-5-20250929
# OM_LLM_REFLECTOR_MODEL=claude-sonnet-4-5-20250929
#
# Direct provider keys (legacy/default flow):
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
#
# Anthropic on Google Vertex AI:
# OM_VERTEX_PROJECT_ID=my-gcp-project
# OM_VERTEX_REGION=us-east5
#
# Anthropic on Amazon Bedrock:
# OM_BEDROCK_REGION=us-east-1
# AWS_REGION=us-east-1

# Search backend: bm25 (default), qmd, qmd-hybrid, none
# OM_SEARCH_BACKEND=bm25
#
# In-session checkpointing (UserPromptSubmit/PreCompact hooks)
# OM_SESSION_OBSERVER_INTERVAL_SECONDS=900  # 15 minutes
# Set to 1/true/yes to disable in-session checkpoints (SessionEnd/Stop still run immediately)
# OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=0

# Codex observer polling cadence (minutes)
# OM_CODEX_OBSERVER_INTERVAL_MINUTES=15
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
    llm_provider: str = field(
        default_factory=lambda: os.environ.get("OM_LLM_PROVIDER", "auto")
    )  # auto|anthropic|openai|anthropic-vertex|anthropic-bedrock
    llm_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_MODEL"))
    llm_observer_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_OBSERVER_MODEL"))
    llm_reflector_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_REFLECTOR_MODEL"))
    anthropic_model: str = field(
        default_factory=lambda: os.environ.get("OM_ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
    )
    openai_model: str = field(default_factory=lambda: os.environ.get("OM_OPENAI_MODEL", "gpt-4o-mini"))
    vertex_project_id: str | None = field(default_factory=lambda: os.environ.get("OM_VERTEX_PROJECT_ID"))
    vertex_region: str | None = field(default_factory=lambda: os.environ.get("OM_VERTEX_REGION"))
    bedrock_region: str | None = field(
        default_factory=lambda: os.environ.get("OM_BEDROCK_REGION") or os.environ.get("AWS_REGION")
    )

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

    def resolve_provider(self) -> str:
        """Resolve active provider using explicit config or legacy key auto-detect."""
        provider = (self.llm_provider or "auto").strip().lower()
        allowed = {"auto", "anthropic", "openai", "anthropic-vertex", "anthropic-bedrock"}
        if provider not in allowed:
            raise RuntimeError(
                "Invalid OM_LLM_PROVIDER="
                f"{provider!r}. Use one of: auto, anthropic, openai, anthropic-vertex, anthropic-bedrock."
            )

        if provider != "auto":
            return provider

        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        raise RuntimeError(
            "No LLM provider resolved. Either set OM_LLM_PROVIDER explicitly, "
            "or add ANTHROPIC_API_KEY / OPENAI_API_KEY "
            f"to {self.env_file}."
        )

    def detect_provider(self) -> str:
        """Backward-compatible alias for provider resolution."""
        return self.resolve_provider()

    def resolve_model(self, operation: str | None = None, provider: str | None = None) -> str:
        """Resolve model for observer/reflector with override precedence."""
        if operation == "observer" and self.llm_observer_model:
            return self.llm_observer_model
        if operation == "reflector" and self.llm_reflector_model:
            return self.llm_reflector_model
        if self.llm_model:
            return self.llm_model

        active_provider = provider or self.resolve_provider()
        if active_provider == "openai":
            return self.openai_model
        if active_provider in {"anthropic", "anthropic-vertex", "anthropic-bedrock"}:
            return self.anthropic_model
        raise RuntimeError(f"Unknown provider for model resolution: {active_provider}")

    def validate_provider_config(self, provider: str | None = None) -> str:
        """Validate provider-specific required settings and return resolved provider."""
        active_provider = provider or self.resolve_provider()

        if active_provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("Provider 'anthropic' requires ANTHROPIC_API_KEY.")
        elif active_provider == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("Provider 'openai' requires OPENAI_API_KEY.")
        elif active_provider == "anthropic-vertex":
            missing = []
            if not self.vertex_project_id:
                missing.append("OM_VERTEX_PROJECT_ID")
            if not self.vertex_region:
                missing.append("OM_VERTEX_REGION")
            if missing:
                raise RuntimeError(
                    "Provider 'anthropic-vertex' is missing required settings: " + ", ".join(missing) + "."
                )
        elif active_provider == "anthropic-bedrock":
            if not self.bedrock_region:
                raise RuntimeError("Provider 'anthropic-bedrock' requires OM_BEDROCK_REGION (or AWS_REGION).")
        else:
            raise RuntimeError(f"Unknown provider: {active_provider}")

        return active_provider

    # --- Cursor (bookmark) management ---

    def load_cursor(self) -> dict:
        """Load the bookmark file tracking last-processed positions."""
        if self.cursor_path.exists():
            return json.loads(self.cursor_path.read_text())
        return {}

    def save_cursor(self, cursor: dict) -> None:
        self.ensure_memory_dir()
        self.cursor_path.write_text(json.dumps(cursor, indent=2) + "\n")
