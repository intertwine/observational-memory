"""Paths, defaults, and environment detection."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def is_windows() -> bool:
    """Return True when running on Windows."""
    return sys.platform == "win32" or os.name == "nt"


def _windows_data_home() -> Path:
    """Return the per-user data directory on Windows.

    Honors LOCALAPPDATA when set, falling back to %USERPROFILE%/AppData/Local.
    Used for files that should not roam across machines (caches, logs).
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data)
    return Path.home() / "AppData" / "Local"


def _windows_config_home() -> Path:
    """Return the per-user roaming config directory on Windows.

    Honors APPDATA when set, falling back to %USERPROFILE%/AppData/Roaming.
    Used for configuration that may roam between machines (env file).
    """
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data)
    return Path.home() / "AppData" / "Roaming"


def _xdg_data_home() -> Path:
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit)
    if is_windows():
        return _windows_data_home()
    return Path.home() / ".local" / "share"


def _xdg_config_home() -> Path:
    explicit = os.environ.get("XDG_CONFIG_HOME")
    if explicit:
        return Path(explicit)
    if is_windows():
        return _windows_config_home()
    return Path.home() / ".config"


def _claude_user_dir() -> Path:
    """Return the Claude Code per-user directory.

    Claude Code uses ``~/.claude`` on every supported platform (it expands
    ``~`` to ``%USERPROFILE%`` on Windows), so we honor that convention.
    """
    return Path.home() / ".claude"


def _codex_user_dir() -> Path:
    """Return the Codex CLI per-user directory."""
    return Path.home() / ".codex"


def _cowork_app_support_dir() -> Path:
    """Return the directory containing Cowork local-agent-mode session/plugin trees.

    Cowork ships only on macOS today. On Windows we point at ``%APPDATA%``
    so that path resolution itself doesn't crash; the install command is
    still gated to skip the actual copy on Windows. Everywhere else we
    keep the macOS-native path so the call is a no-op on Linux too.
    """
    if is_windows():
        return _windows_config_home() / "Claude"
    return Path.home() / "Library" / "Application Support" / "Claude"


def _has_subscription_tokens(provider_id: str, auth_path: Path | None = None) -> bool:
    """Lightweight, side-effect-free check for stored subscription tokens.

    Avoids importing the auth module at config-load time (which would create
    a circular import). Reads auth.json directly; missing/malformed → False.

    When ``auth_path`` is given (Config methods pass the store path relative to
    the active env file) it is authoritative. Otherwise we honor ``OM_AUTH_FILE``
    and fall back to the default XDG location — used by runtime callers that
    don't have a Config in hand.
    """
    import json as _json

    if auth_path is not None:
        path = auth_path
    else:
        override = os.environ.get("OM_AUTH_FILE")
        if override:
            path = Path(override).expanduser()
        else:
            path = _xdg_config_home() / "observational-memory" / "auth.json"
    try:
        if not path.is_file():
            return False
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return False
    state = providers.get(provider_id)
    if not isinstance(state, dict):
        return False
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        return False
    return bool(str(tokens.get("access_token") or "").strip() or str(tokens.get("refresh_token") or "").strip())


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value.strip().replace("_", "").replace(",", ""))
    except (AttributeError, ValueError):
        return default


# Valid OM_REFLECTOR_STRATEGY values (see Config.reflector_strategy).
REFLECTOR_STRATEGIES = ("legacy", "sectioned", "auto")


def _safe_strategy(value: str | None, default: str) -> str:
    """Normalize OM_REFLECTOR_STRATEGY, falling back to *default* on bad input."""
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized if normalized in REFLECTOR_STRATEGIES else default


ENV_FILE_TEMPLATE = """\
# Observational Memory — API Keys
# This file is sourced by om, its hooks, and its background scheduler jobs.
# It is NOT committed to any repo. Keep it private.
#
# LLM provider selection (auto rule order: anthropic key, openai key,
# openai-chatgpt subscription, xai-oauth subscription, xai api key):
# OM_LLM_PROVIDER=auto  # auto|anthropic|openai|anthropic-vertex|anthropic-bedrock|openai-chatgpt|xai-oauth|xai
#
# Shared/default model for observer + reflector:
# OM_LLM_MODEL=claude-sonnet-4-5-20250929
#
# Optional per-step model overrides:
# OM_LLM_OBSERVER_MODEL=claude-sonnet-4-5-20250929
# OM_LLM_REFLECTOR_MODEL=claude-sonnet-4-5-20250929
#
# Optional per-step PROVIDER overrides (run a fast model for observe and a
# strong model for reflect). When set, that workflow uses this provider
# directly and its model resolves from the per-step model override or the
# provider default (NOT the global OM_LLM_MODEL):
# OM_LLM_OBSERVER_PROVIDER=xai-oauth
# OM_LLM_REFLECTOR_PROVIDER=openai-chatgpt
#
# Observer context budget: how many chars of existing observations.md are
# re-sent for dedup context each run (0 = send all; default 12000):
# OM_OBSERVER_CONTEXT_MAX_CHARS=12000
# Reflector context budget: how many chars of existing reflections.md are
# re-sent to the reflector (0 = send all; default 48000, comfortably fits a
# target-size doc; bounds the chunked fold's re-send cost):
# OM_REFLECTOR_CONTEXT_MAX_CHARS=48000
# Reflector per-call input ceiling in tokens (~3.5 chars/token). Sized so the
# configured OM_REFLECTOR_CONTEXT_MAX_CHARS actually binds rather than being
# silently clamped by the input ceiling (default 45000):
# OM_REFLECTOR_MAX_INPUT_TOKENS=45000
# Fraction of the reflector's per-call budget given to the observations chunk;
# the rest is the reflections context. Higher = fewer folds (default 0.6):
# OM_REFLECTOR_OBSERVATION_CHUNK_RATIO=0.6
# Reflector output cap: hard ceiling (chars) on the reflector's emitted document,
# applied post-call so it also bounds the openai-chatgpt (Codex) path, which
# rejects max_output_tokens. Generous default; trims at a section boundary and
# warns only on a runaway. 0 disables:
# OM_REFLECTOR_OUTPUT_MAX_CHARS=200000
# Reflector folding strategy: legacy | sectioned | auto (default auto). 'legacy'
# re-sends a bounded prefix of the running document on every chunked fold;
# 'sectioned' routes each observation chunk to the reflection sections it touches
# and re-sends only those plus the durable core bundle (Milestone 3, scales to
# large memory); 'auto' uses legacy for small corpora and sectioned once the
# document outgrows a single fold's input budget:
# OM_REFLECTOR_STRATEGY=auto
#
# Startup context: mark operational facts (tool versions/install status) older
# than this many days with "(as of <date> — verify)" (default 14):
# OM_STARTUP_FRESHNESS_DAYS=14
#
# ChatGPT Codex reasoning effort (low|medium|high|xhigh). Lower = faster/cheaper.
# Default: observer=low, reflector=backend default. Per-op overrides win:
# OM_OPENAI_CHATGPT_REASONING_EFFORT=
# OM_OPENAI_CHATGPT_OBSERVER_REASONING_EFFORT=low
# OM_OPENAI_CHATGPT_REFLECTOR_REASONING_EFFORT=
#
# Direct provider keys (legacy/default flow):
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# XAI_API_KEY=xai-...
#
# Offline reflection via OpenAI Batch (API-key 'openai' provider only; ~50%
# cheaper, 24h window). off|batch. When 'batch', `om reflect` submits a Batch
# job; apply later with `om jobs poll`. Also opt-in per run via `om reflect --async`.
# OM_OPENAI_ASYNC_MODE=off
#
# Subscription providers (preferred for cheap-feeling features; run `om login`):
# OM_OPENAI_CHATGPT_MODEL=gpt-5.5
# OM_XAI_OAUTH_MODEL=grok-4.3
# OM_XAI_MODEL=grok-4.3
# Endpoint overrides (validated against chatgpt.com / api.x.ai):
# OM_OPENAI_CHATGPT_BASE_URL=
# OM_XAI_OAUTH_BASE_URL=
# OM_XAI_BASE_URL=
# Client-id overrides (if you mint your own OAuth client):
# OM_OPENAI_CHATGPT_CLIENT_ID=
# OM_XAI_OAUTH_CLIENT_ID=
#
# Anthropic on Google Vertex AI:
# OM_VERTEX_PROJECT_ID=my-gcp-project
# OM_VERTEX_REGION=us-east5
#
# Anthropic on Amazon Bedrock:
# OM_BEDROCK_REGION=us-east-1
# AWS_REGION=us-east-1

# Search backend: bm25 (default), qmd, qmd-hybrid, moss, none
# OM_SEARCH_BACKEND=bm25
# OM_QMD_INDEX_NAME=observational-memory
# OM_QMD_NO_RERANK=0
# OM_QMD_EMBED_MODEL=
# OM_QMD_RERANK_MODEL=
# OM_QMD_GENERATE_MODEL=
#
# Moss backend (https://www.moss.dev) — OPT-IN. Enabling it uploads memory text
# to the Moss cloud (service.usemoss.dev); scope=local sections are withheld.
# OM_MOSS_PROJECT_ID=
# OM_MOSS_PROJECT_KEY=          # secret — never committed or logged
# OM_MOSS_INDEX_NAME=observational-memory
# OM_MOSS_MODEL_ID=             # blank = SDK default (moss-minilm)
# OM_MOSS_ALPHA=                # blank = SDK default; hybrid keyword/vector blend
#
# In-session checkpointing (UserPromptSubmit/PreCompact hooks)
# OM_SESSION_OBSERVER_INTERVAL_SECONDS=900  # 15 minutes
# Set to 1/true/yes to disable in-session checkpoints (SessionEnd/Stop still run immediately)
# OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=0

# Codex observer polling cadence (minutes)
# OM_CODEX_OBSERVER_INTERVAL_MINUTES=15

# Usage tracking, cost estimation, and budgets (host-local; never synced).
# Records every LLM call to usage.sqlite next to your memory files. Inspect with
# `om usage status` / `om usage tail`; configure with `om usage budget`.
# OM_USAGE_TRACKING=1                  # 1=on (default), 0=off (no DB, no overhead)
# OM_USAGE_DB=                         # override DB path (default: <memory_dir>/usage.sqlite)
# OM_PRICING_OVERRIDES=                # pricing overrides (default: <config_dir>/pricing.toml)
#
# Budgets: OM_BUDGET_[<OPERATION>_]<WINDOW>_<UNIT>
#   OPERATION (optional): OBSERVER | REFLECTOR   WINDOW: DAILY|MONTHLY|SESSION   UNIT: USD|TOKENS
# OM_BUDGET_DAILY_USD=5.00
# OM_BUDGET_MONTHLY_USD=100.00
# OM_BUDGET_REFLECTOR_DAILY_USD=1.00
# OM_BUDGET_DAILY_TOKENS=2_000_000
# OM_BUDGET_MODE=hard                  # hard (block) | soft (warn); per-budget override: <KEY>_MODE
# OM_BUDGET_SOFT_THRESHOLD=0.8         # warn at 80% of a cap
# OM_BUDGET_BYPASS=0                   # one-shot: 1 lets a single call exceed a hard cap
"""


@dataclass
class Config:
    """Runtime configuration — resolved from env vars and defaults."""

    CODEX_OBSERVE_LAUNCHD_LABEL = "com.intertwine.observational-memory.codex-observe"
    AUTO_MEMORY_LAUNCHD_LABEL = "com.intertwine.observational-memory.auto-memory"
    REFLECT_LAUNCHD_LABEL = "com.intertwine.observational-memory.reflect"

    # Windows Task Scheduler task names mirror the launchd labels for parity
    # across platforms — the stable identifier is the bare label.
    CODEX_OBSERVE_SCHTASKS_NAME = CODEX_OBSERVE_LAUNCHD_LABEL
    AUTO_MEMORY_SCHTASKS_NAME = AUTO_MEMORY_LAUNCHD_LABEL
    REFLECT_SCHTASKS_NAME = REFLECT_LAUNCHD_LABEL

    # Memory storage
    memory_dir: Path = field(default_factory=lambda: _xdg_data_home() / "observational-memory")

    # Env file for API keys
    env_file: Path = field(default_factory=lambda: _xdg_config_home() / "observational-memory" / "env")

    # Claude Code paths
    claude_projects_dir: Path = field(default_factory=lambda: _claude_user_dir() / "projects")
    claude_settings_path: Path = field(default_factory=lambda: _claude_user_dir() / "settings.json")

    # Codex CLI paths
    codex_home: Path = field(default_factory=lambda: Path(os.environ.get("CODEX_HOME", _codex_user_dir())))

    # LLM settings
    llm_provider: str = field(
        default_factory=lambda: os.environ.get("OM_LLM_PROVIDER", "auto")
    )  # auto|anthropic|openai|anthropic-vertex|anthropic-bedrock|openai-chatgpt|xai-oauth|xai
    llm_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_MODEL"))
    llm_observer_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_OBSERVER_MODEL"))
    llm_reflector_model: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_REFLECTOR_MODEL"))
    # Per-workflow provider overrides. When set, that operation uses the named
    # provider directly (no model-name inference), and its model resolves from
    # the per-step model override or that provider's default — NOT the global
    # OM_LLM_MODEL (which usually belongs to a different provider). Lets you run
    # e.g. a fast model for observe and a strong model for reflect.
    llm_observer_provider: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_OBSERVER_PROVIDER"))
    llm_reflector_provider: str | None = field(default_factory=lambda: os.environ.get("OM_LLM_REFLECTOR_PROVIDER"))
    anthropic_model: str = field(
        default_factory=lambda: os.environ.get("OM_ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
    )
    openai_model: str = field(default_factory=lambda: os.environ.get("OM_OPENAI_MODEL", "gpt-4o-mini"))
    # The ChatGPT-account Codex allow-list shifts over time (gpt-5-codex was
    # rejected with HTTP 400 on 2026-05-23; the live /models endpoint listed
    # gpt-5.5 / gpt-5.4 / gpt-5.3-codex). gpt-5.5 is the current flagship;
    # override with OM_OPENAI_CHATGPT_MODEL when the allow-list moves.
    openai_chatgpt_model: str = field(default_factory=lambda: os.environ.get("OM_OPENAI_CHATGPT_MODEL", "gpt-5.5"))
    # ChatGPT Codex reasoning effort (low|medium|high|xhigh). Lower effort cuts
    # gpt-5.5 latency sharply. The global default applies to all Codex calls; the
    # per-operation overrides win. Built-in default: observer="low" (frequent,
    # latency-sensitive), reflector unset (omit -> backend default, to preserve
    # consolidation quality). See resolve_reasoning_effort().
    codex_reasoning_effort: str | None = field(
        default_factory=lambda: os.environ.get("OM_OPENAI_CHATGPT_REASONING_EFFORT")
    )
    codex_observer_reasoning_effort: str | None = field(
        default_factory=lambda: os.environ.get("OM_OPENAI_CHATGPT_OBSERVER_REASONING_EFFORT")
    )
    codex_reflector_reasoning_effort: str | None = field(
        default_factory=lambda: os.environ.get("OM_OPENAI_CHATGPT_REFLECTOR_REASONING_EFFORT")
    )
    xai_oauth_model: str = field(default_factory=lambda: os.environ.get("OM_XAI_OAUTH_MODEL", "grok-4.3"))
    xai_model: str = field(default_factory=lambda: os.environ.get("OM_XAI_MODEL", "grok-4.3"))
    vertex_project_id: str | None = field(default_factory=lambda: os.environ.get("OM_VERTEX_PROJECT_ID"))
    vertex_region: str | None = field(default_factory=lambda: os.environ.get("OM_VERTEX_REGION"))
    bedrock_region: str | None = field(
        default_factory=lambda: os.environ.get("OM_BEDROCK_REGION") or os.environ.get("AWS_REGION")
    )

    # Observer settings
    min_messages: int = 5  # skip if fewer new messages
    # Cap on how much of the existing observations.md is sent back to the
    # observer for dedup/continuity context. Without a cap, every observe
    # re-sends the entire (unboundedly growing) file — the dominant cost on the
    # most frequent operation. We send only the most recent tail; 0 disables
    # the cap (legacy behavior: send everything).
    #
    # Only applied in OM Cluster mode, where observations are stored as an
    # append-only record log and observations.md is a materialized view (older
    # observations live in older records, so a bounded context can't lose data).
    # In non-cluster mode the observer rewrites the whole file, so the full
    # existing content is always sent to avoid dropping history. See #52 for the
    # append-only contract change that would let non-cluster mode bound this too.
    observer_context_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("OM_OBSERVER_CONTEXT_MAX_CHARS", "12000"))
    )
    # Cap on how much of the existing reflections.md is re-sent to the reflector
    # as "current reflections" context. The chunked reflector folds each chunk
    # into a running document and re-sends it every fold; without a bound that is
    # O(chunks x reflections_size). The default comfortably fits a target-size
    # reflections.md (the prompt aims for 200-600 lines, ~48k chars at the top of
    # that range), so the cap only trims documents grown past target — keeping the
    # head, where durable identity/projects live, and logging a warning. 0 disables.
    reflector_context_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("OM_REFLECTOR_CONTEXT_MAX_CHARS", "48000"))
    )
    # Maximum input tokens to send in a single reflector call. The chunked
    # reflector keeps every fold (system prompt + reflections context + obs
    # chunk + wrappers) under this ceiling, converted to chars via the
    # reflector's ~3.5 chars/token estimate.
    #
    # The default is sized so the *effective* reflections context cap is not
    # silently clamped below the configured OM_REFLECTOR_CONTEXT_MAX_CHARS
    # (default 48000). The chunked path gives observations the chunk ratio
    # (default 0.6) of the budget and leaves the rest for reflections, minus
    # the system prompt (~4.3k chars), auto-memory section (up to ~4k), and a
    # fixed wrapper allowance (400). At 45000 tokens:
    #   45000 * 3.5 = 157500 chars total
    #   157500 * (1 - 0.6) - 4312 - 4000 - 400 = ~54288 chars for reflections
    # which comfortably exceeds the 48000 configured cap, so the configured cap
    # binds (not the input ceiling) for the current corpus shape. 0 is invalid
    # (a positive ceiling is required); raise this to send more per call.
    reflector_max_input_tokens: int = field(
        default_factory=lambda: int(os.environ.get("OM_REFLECTOR_MAX_INPUT_TOKENS", "45000"))
    )
    # Fraction of the per-call input budget the chunked reflector allocates to
    # the observations chunk; the rest goes to the reflections context. Larger
    # chunks mean fewer folds, and each fold re-sends the reflections context,
    # so a higher ratio minimizes the repeated re-send cost. Range (0, 1).
    reflector_observation_chunk_ratio: float = field(
        default_factory=lambda: _safe_float(os.environ.get("OM_REFLECTOR_OBSERVATION_CHUNK_RATIO"), 0.6)
    )
    # Operator-side cap on the reflector's *output* size, applied post-call so it
    # works on every backend — including the openai-chatgpt (Codex) Responses path,
    # which rejects max_output_tokens. The default is deliberately generous: a
    # target-size reflections.md (200-600 lines) is well under this, and the
    # pathological run that motivated the cap emitted ~121k chars, so the default
    # only fires on a genuine runaway. When the output overruns, the reflect
    # pipeline trims at a section ("## ") boundary — never mid-section, which would
    # corrupt reflections.md — and logs a warning. 0 disables the cap.
    reflector_output_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("OM_REFLECTOR_OUTPUT_MAX_CHARS", "200000"))
    )
    # Reflector folding strategy: how the reflector folds new observations into
    # the existing reflections.md.
    #   legacy    — single-pass when small, else chunked: each chunked fold
    #               re-sends a (bounded) prefix of the running document. Simple,
    #               but the re-sent prefix is O(chunks x reflections-size) and
    #               head-truncates once the document outgrows one fold's budget.
    #   sectioned — section-targeted (Milestone 3): route each observation chunk
    #               to the reflection sections it touches, send only those (plus
    #               an always-visible durable core bundle), and reassemble the
    #               full document byte-for-byte from the unchanged sections. Per-
    #               fold resend is proportional to the touched sections, not the
    #               whole document.
    #   auto      — default: legacy for small corpora (the document fits in one
    #               fold's input budget), sectioned once it grows past that, where
    #               legacy would otherwise head-truncate every fold.
    reflector_strategy: str = field(
        default_factory=lambda: _safe_strategy(os.environ.get("OM_REFLECTOR_STRATEGY"), "auto")
    )

    # Reflector settings
    observation_retention_days: int = 7
    reflections_target_lines: int = 400  # aim for 200-600
    snapshot_ttl_days: int = field(default_factory=lambda: int(os.environ.get("OM_SNAPSHOT_TTL_DAYS", "14")))
    snapshot_expiry_action: str = field(
        default_factory=lambda: os.environ.get("OM_SNAPSHOT_EXPIRY_ACTION", "stale-section")
    )

    # Search settings
    search_backend: str = field(
        default_factory=lambda: os.environ.get("OM_SEARCH_BACKEND", "bm25")
    )  # "bm25" | "qmd" | "qmd-hybrid" | "none"
    qmd_index_name: str = field(default_factory=lambda: os.environ.get("OM_QMD_INDEX_NAME", "observational-memory"))
    qmd_no_rerank: bool = field(default_factory=lambda: _env_flag("OM_QMD_NO_RERANK", False))
    qmd_embed_model: str | None = field(default_factory=lambda: os.environ.get("OM_QMD_EMBED_MODEL"))
    qmd_rerank_model: str | None = field(default_factory=lambda: os.environ.get("OM_QMD_RERANK_MODEL"))
    qmd_generate_model: str | None = field(default_factory=lambda: os.environ.get("OM_QMD_GENERATE_MODEL"))

    # Moss search backend (cloud-backed semantic search; opt-in). Indexing
    # UPLOADS memory text to service.usemoss.dev — never enabled by default, and
    # the backend drops scope=local entries before upload. The project key is a
    # secret and is never logged or printed.
    moss_project_id: str | None = field(default_factory=lambda: os.environ.get("OM_MOSS_PROJECT_ID") or None)
    moss_project_key: str | None = field(default_factory=lambda: os.environ.get("OM_MOSS_PROJECT_KEY") or None)
    moss_index_name: str = field(default_factory=lambda: os.environ.get("OM_MOSS_INDEX_NAME", "observational-memory"))
    moss_model_id: str | None = field(default_factory=lambda: os.environ.get("OM_MOSS_MODEL_ID") or None)
    moss_alpha: float | None = field(
        default_factory=lambda: _safe_float(os.environ.get("OM_MOSS_ALPHA"), None)  # type: ignore[arg-type]
    )

    # Usage tracking, cost estimation, and budgets (host-local; never synced).
    # The DB and pricing override paths are derived properties below so that a
    # custom memory_dir / env_file (e.g. tmp_path in tests) keeps them local too.
    usage_tracking: bool = field(default_factory=lambda: _env_flag("OM_USAGE_TRACKING", True))
    budget_mode: str = field(default_factory=lambda: os.environ.get("OM_BUDGET_MODE", "hard"))
    budget_soft_threshold: float = field(
        default_factory=lambda: _safe_float(os.environ.get("OM_BUDGET_SOFT_THRESHOLD"), 0.8)
    )
    # Async execution mode for the direct API-key OpenAI provider: off | batch.
    # When "batch", `om reflect` submits an offline OpenAI Batch job instead of
    # running synchronously (also opt-in per-invocation via `om reflect --async`).
    # Never applies to the openai-chatgpt subscription provider.
    openai_async_mode: str = field(default_factory=lambda: os.environ.get("OM_OPENAI_ASYNC_MODE", "off"))

    @property
    def observations_path(self) -> Path:
        return self.memory_dir / "observations.md"

    @property
    def reflections_path(self) -> Path:
        return self.memory_dir / "reflections.md"

    @property
    def profile_path(self) -> Path:
        return self.memory_dir / "profile.md"

    @property
    def active_path(self) -> Path:
        return self.memory_dir / "active.md"

    @property
    def cursor_path(self) -> Path:
        return self.memory_dir / ".cursor.json"

    @property
    def search_index_dir(self) -> Path:
        return self.memory_dir / ".search-index"

    @property
    def usage_db_path(self) -> Path:
        """Host-local usage SQLite DB. Never materialized or synced via OM Cluster."""
        override = os.environ.get("OM_USAGE_DB")
        if override:
            return Path(override).expanduser()
        return self.memory_dir / "usage.sqlite"

    @property
    def pricing_overrides_path(self) -> Path:
        """Optional per-host pricing override file (wins over the shipped snapshot)."""
        override = os.environ.get("OM_PRICING_OVERRIDES")
        if override:
            return Path(override).expanduser()
        return self.env_file.parent / "pricing.toml"

    @property
    def provider_jobs_dir(self) -> Path:
        """Host-local store for async provider jobs (e.g. OpenAI Batch). Never synced."""
        return self.memory_dir / ".provider-jobs"

    @property
    def openai_batch_jobs_dir(self) -> Path:
        return self.provider_jobs_dir / "openai-batch"

    @property
    def auth_file(self) -> Path:
        """Path to the subscription auth store (next to the env file).

        Honors OM_AUTH_FILE for tests; otherwise lives beside the env file so a
        custom env_file (e.g. a tmp_path in tests) keeps the store local too.
        """
        override = os.environ.get("OM_AUTH_FILE")
        if override:
            return Path(override).expanduser()
        return self.env_file.parent / "auth.json"

    @property
    def cluster_config_path(self) -> Path:
        return self.env_file.parent / "cluster.toml"

    @property
    def cluster_keys_dir(self) -> Path:
        return self.env_file.parent / "cluster-keys"

    @property
    def clusters_dir(self) -> Path:
        return self.memory_dir / "clusters"

    @property
    def codex_agents_md(self) -> Path:
        return self.codex_home / "AGENTS.md"

    @property
    def codex_config_path(self) -> Path:
        return self.codex_home / "config.toml"

    @property
    def codex_hooks_path(self) -> Path:
        return self.codex_home / "hooks.json"

    @property
    def hermes_sessions_dir(self) -> Path:
        return Path.home() / ".hermes" / "sessions"

    # Grok Build TUI paths (xAI)
    grok_home: Path = field(default_factory=lambda: Path(os.environ.get("GROK_HOME", Path.home() / ".grok")))

    @property
    def grok_config_path(self) -> Path:
        return self.grok_home / "config.toml"

    @property
    def grok_hooks_dir(self) -> Path:
        return self.grok_home / "hooks"

    @property
    def grok_sessions_dir(self) -> Path:
        return self.grok_home / "sessions"

    @property
    def cowork_sessions_dir(self) -> Path:
        return _cowork_app_support_dir() / "local-agent-mode-sessions"

    @property
    def cowork_plugins_dir(self) -> Path:
        return _cowork_app_support_dir() / "local-agent-mode-plugins"

    @property
    def codex_checkpoint_state_path(self) -> Path:
        return self.memory_dir / ".codex-checkpoint-state.json"

    @property
    def codex_checkpoint_lock_dir(self) -> Path:
        return self.memory_dir / ".codex-checkpoint-locks"

    # Claude Code session-end / checkpoint state. Names match the bash
    # session-end.sh hook so a host that switches from POSIX to Windows
    # picks up the existing state file in place.
    @property
    def claude_checkpoint_state_path(self) -> Path:
        return self.memory_dir / ".session-observer-state.json"

    @property
    def claude_checkpoint_lock_dir(self) -> Path:
        return self.memory_dir / ".session-observer-locks"

    @property
    def launch_agents_dir(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents"

    @property
    def scheduler_log_dir(self) -> Path:
        return self.memory_dir / ".scheduler-logs"

    @property
    def codex_observe_launchd_plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.CODEX_OBSERVE_LAUNCHD_LABEL}.plist"

    @property
    def auto_memory_launchd_plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.AUTO_MEMORY_LAUNCHD_LABEL}.plist"

    @property
    def reflect_launchd_plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.REFLECT_LAUNCHD_LABEL}.plist"

    @property
    def codex_observe_launchd_stdout_path(self) -> Path:
        return self.scheduler_log_dir / "codex-observe.out.log"

    @property
    def codex_observe_launchd_stderr_path(self) -> Path:
        return self.scheduler_log_dir / "codex-observe.err.log"

    @property
    def auto_memory_launchd_stdout_path(self) -> Path:
        return self.scheduler_log_dir / "auto-memory.out.log"

    @property
    def auto_memory_launchd_stderr_path(self) -> Path:
        return self.scheduler_log_dir / "auto-memory.err.log"

    @property
    def reflect_launchd_stdout_path(self) -> Path:
        return self.scheduler_log_dir / "reflect.out.log"

    @property
    def reflect_launchd_stderr_path(self) -> Path:
        return self.scheduler_log_dir / "reflect.err.log"

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
        if not is_windows():
            # chmod 600 is a no-op on Windows; per-user APPDATA already
            # restricts access via NTFS ACLs to the current user.
            self.env_file.chmod(0o600)
        return True

    def resolve_provider(self) -> str:
        """Resolve active provider using explicit config or legacy key auto-detect."""
        provider = (self.llm_provider or "auto").strip().lower()
        allowed = {
            "auto",
            "anthropic",
            "openai",
            "anthropic-vertex",
            "anthropic-bedrock",
            "openai-chatgpt",
            "xai-oauth",
            "xai",
        }
        if provider not in allowed:
            raise RuntimeError(f"Invalid OM_LLM_PROVIDER={provider!r}. Use one of: " + ", ".join(sorted(allowed)) + ".")

        if provider != "auto":
            return provider

        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if _has_subscription_tokens("openai-chatgpt", self.auth_file):
            return "openai-chatgpt"
        if _has_subscription_tokens("xai-oauth", self.auth_file):
            return "xai-oauth"
        if os.environ.get("XAI_API_KEY"):
            return "xai"
        raise RuntimeError(
            "No LLM provider resolved. Either set OM_LLM_PROVIDER explicitly, "
            "add ANTHROPIC_API_KEY / OPENAI_API_KEY / XAI_API_KEY "
            f"to {self.env_file}, or run `om login` to authenticate via your "
            "ChatGPT / xAI subscription."
        )

    def detect_provider(self) -> str:
        """Backward-compatible alias for provider resolution."""
        return self.resolve_provider()

    def operation_provider(self, operation: str | None) -> str | None:
        """Return an explicit per-workflow provider override, if set.

        ``OM_LLM_OBSERVER_PROVIDER`` / ``OM_LLM_REFLECTOR_PROVIDER`` let the user
        pin a specific provider per workflow. Returns None when no override
        applies (the caller then falls back to the default + name inference).
        """
        override = None
        if operation == "observer" and self.llm_observer_provider:
            override = self.llm_observer_provider.strip().lower()
        elif operation == "reflector" and self.llm_reflector_provider:
            override = self.llm_reflector_provider.strip().lower()
        # "auto" is not a real per-workflow target — treat it as no override so
        # the caller falls back to normal resolution.
        if not override or override == "auto":
            return None
        return override

    def resolve_reasoning_effort(self, operation: str | None) -> str | None:
        """Resolve the ChatGPT Codex reasoning effort for an operation.

        Precedence: per-operation env override -> global
        ``OM_OPENAI_CHATGPT_REASONING_EFFORT`` -> built-in default
        (``observer`` -> ``"low"``; anything else -> ``None`` = omit the
        parameter and use the backend default). Returns ``None`` to omit it.
        Unrecognized values are ignored (omitted) so a typo can't hard-fail a
        call — only ``low|medium|high|xhigh`` are forwarded.
        """
        allowed = {"low", "medium", "high", "xhigh"}
        if operation == "observer" and self.codex_observer_reasoning_effort:
            effort = self.codex_observer_reasoning_effort
        elif operation == "reflector" and self.codex_reflector_reasoning_effort:
            effort = self.codex_reflector_reasoning_effort
        elif self.codex_reasoning_effort:
            effort = self.codex_reasoning_effort
        elif operation == "observer":
            effort = "low"
        else:
            return None
        effort = (effort or "").strip().lower()
        return effort if effort in allowed else None

    def resolve_model(
        self,
        operation: str | None = None,
        provider: str | None = None,
        *,
        ignore_global_model: bool = False,
    ) -> str:
        """Resolve model for observer/reflector with override precedence.

        ``ignore_global_model`` skips the global ``OM_LLM_MODEL`` so an explicit
        per-workflow provider falls through to the per-step model override or the
        provider's own default, rather than a global model meant for a different
        provider.
        """
        if operation == "observer" and self.llm_observer_model:
            return self.llm_observer_model
        if operation == "reflector" and self.llm_reflector_model:
            return self.llm_reflector_model
        if self.llm_model and not ignore_global_model:
            return self.llm_model

        active_provider = provider or self.resolve_provider()
        if active_provider == "openai":
            return self.openai_model
        if active_provider == "openai-chatgpt":
            return self.openai_chatgpt_model
        if active_provider == "xai-oauth":
            return self.xai_oauth_model
        if active_provider == "xai":
            return self.xai_model
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
        elif active_provider == "openai-chatgpt":
            if not _has_subscription_tokens("openai-chatgpt", self.auth_file):
                raise RuntimeError(
                    "Provider 'openai-chatgpt' requires ChatGPT subscription tokens. Run `om login openai-chatgpt`."
                )
        elif active_provider == "xai-oauth":
            if not _has_subscription_tokens("xai-oauth", self.auth_file):
                raise RuntimeError("Provider 'xai-oauth' requires xAI subscription tokens. Run `om login xai-oauth`.")
        elif active_provider == "xai":
            if not os.environ.get("XAI_API_KEY"):
                raise RuntimeError("Provider 'xai' requires XAI_API_KEY.")
        else:
            raise RuntimeError(f"Unknown provider: {active_provider}")

        return active_provider

    def qmd_model_env(self) -> dict[str, str]:
        """Return configured QMD model overrides for subprocess execution."""
        env: dict[str, str] = {}
        if self.qmd_embed_model:
            env["QMD_EMBED_MODEL"] = self.qmd_embed_model
        if self.qmd_rerank_model:
            env["QMD_RERANK_MODEL"] = self.qmd_rerank_model
        if self.qmd_generate_model:
            env["QMD_GENERATE_MODEL"] = self.qmd_generate_model
        return env

    def moss_credentials(self) -> tuple[str, str] | None:
        """Return (project_id, project_key) if both are configured, else None.

        Never log or print the result — the project key is a secret.
        """
        if self.moss_project_id and self.moss_project_key:
            return (self.moss_project_id, self.moss_project_key)
        return None

    # --- Cursor (bookmark) management ---

    def load_cursor(self) -> dict:
        """Load the bookmark file tracking last-processed positions."""
        if self.cursor_path.exists():
            return json.loads(self.cursor_path.read_text())
        return {}

    def save_cursor(self, cursor: dict) -> None:
        self.ensure_memory_dir()
        self.cursor_path.write_text(json.dumps(cursor, indent=2) + "\n")
