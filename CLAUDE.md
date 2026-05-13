# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test

```bash
# Makefile targets (preferred)
make check              # lint + test (run before committing)
make test               # tests only
make lint               # ruff check + format check
make format             # auto-format code
make build              # build sdist + wheel
make clean              # remove build artifacts
make bump-version       # bump patch (BUMP=minor|major)
make publish-test       # publish to TestPyPI
make publish            # publish to PyPI (production)
make brew-formula       # generate Homebrew formula from PyPI artifacts
make brew-check         # run brew audit on generated formula
make release-homebrew   # copy formula into a local tap checkout
make brew-install       # install from configured Homebrew target
make install-dev        # editable install with dev deps
make doctor             # run om doctor diagnostics

# Direct commands
uv sync                          # install deps
uv run pytest                    # run all tests
uv run pytest tests/test_transcripts.py  # single test file
uv run pytest -v                 # verbose
uv run om status                 # check local installation status
uv run om doctor                 # run diagnostics
```

## Architecture

Cross-agent observational memory that works at the **user level** (not per-project) across Claude Code, Codex CLI, Cowork, and Hermes. Two background processes compress conversation transcripts into shared memory files at `~/.local/share/observational-memory/`.

### Data flow

```text
Transcripts (Claude JSONL / Codex sessions / Cowork audit.jsonl)
  → observe.py (LLM compression → observations.md)
  → reflect.py (daily consolidation → reflections.md)

Auto-memory (~/.claude/projects/*/memory/*.md)
  → observe.py:observe_auto_memory (content-hash scan, NO LLM)
  → search index (DocumentSource.AUTO_MEMORY)
  → reflect.py (supplementary cross-project context)
```

### Key modules

- **`src/observational_memory/transcripts/claude.py`** — Parses Claude Code `.jsonl` transcripts (and Cowork `audit.jsonl` via `source="cowork"`). Each line is a JSON object with `type` (user/assistant/progress), `message.content` (text or array of blocks), `uuid`, `timestamp` (or `_audit_timestamp` for Cowork).
- **`src/observational_memory/transcripts/codex.py`** — Parses Codex CLI session files (`*.json` and `*.jsonl`) from `~/.codex/sessions/`.
- **`src/observational_memory/transcripts/cowork.py`** — Discovery functions for Cowork `audit.jsonl` files under `~/Library/Application Support/Claude/local-agent-mode-sessions/`. Parsing delegates to `claude.py`.
- **`src/observational_memory/transcripts/hermes.py`** — Parses Hermes Agent session JSONL logs from `~/.hermes/sessions/` for manual or plugin-driven observation.
- **`src/observational_memory/transcripts/auto_memory.py`** — Scans Claude Code auto-memory files (`~/.claude/projects/*/memory/*.md`). Content-hash change detection, project slug extraction, YAML frontmatter parsing. Read-only — never writes to auto-memory directories.
- **`src/observational_memory/observe.py`** — Observer: reads transcripts, finds new messages via cursor bookmarks, calls LLM to compress, appends to `observations.md`. Also contains `observe_auto_memory()` which bypasses the LLM (auto-memory files are already distilled) and only updates the search index.
- **`src/observational_memory/reflect.py`** — Reflector: reads observations + reflections + auto-memory context, calls LLM to condense, writes `reflections.md`, trims old observations. Auto-memory is only included when it has changed since last reflection (timestamp comparison). On deletion, the reflector receives cleanup instructions.
- **`src/observational_memory/llm.py`** — Thin abstraction over Anthropic and OpenAI APIs. Auto-detects provider from env vars.
- **`src/observational_memory/config.py`** — All paths, defaults, cursor management. Memory dir follows XDG spec. Search backend is configurable via `OM_SEARCH_BACKEND` env var.
- **`src/observational_memory/cli.py`** — Click CLI (`om` command). Commands: observe, reflect, backfill, search, context, install, uninstall, status, doctor.
- **`src/observational_memory/search/`** — Pluggable search over memory files. Three document sources: `OBSERVATIONS`, `REFLECTIONS`, `AUTO_MEMORY`. BM25 backend (default, uses `rank-bm25`), QMD backend (optional, shells out to `qmd` CLI — `"qmd"` for keyword search, `"qmd-hybrid"` for hybrid BM25 + vector + LLM reranking), None backend (no-op). Parser splits observations by date, reflections by section, auto-memory by file. The `reindex()` orchestrator is called automatically after observe/reflect writes. Auto-memory doc IDs use `amem:<project-slug>/<stem>` prefix.

### Platform support

- **macOS**: launchd-based scheduler (`om install` defaults to `--scheduler launchd`).
- **Linux**: cron-based scheduler (`om install` defaults to `--scheduler cron`).
- **Windows**: Task Scheduler via `schtasks.exe` (`om install` defaults to `--scheduler schtasks`). Memory dir defaults to `%LOCALAPPDATA%\observational-memory\` and the env file to `%APPDATA%\observational-memory\env`. Claude hooks call `om context` and `om claude-checkpoint` directly — no `bash` or `jq` required. Cowork is macOS-only and `om install --cowork` is a no-op on Windows.

### Agent integration

- **Claude Code**: `SessionStart` injects memory via `additionalContext`; `SessionEnd`, `UserPromptSubmit`, and `PreCompact` hooks trigger checkpoints. In-session checkpoints are throttled by `OM_SESSION_OBSERVER_INTERVAL_SECONDS` and can be disabled with `OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS`. On POSIX hosts the hooks run `hooks/claude/session-{start,end}.sh` (which require `bash` + `jq`); on Windows they invoke `om context` and `om claude-checkpoint` directly.
- **Codex CLI**: Instructions appended to `~/.codex/AGENTS.md`; cron job for observer. Startup priming is now driven by derived compact files (`profile.md` + `active.md`) instead of always loading full reflections/observations.
- **Cowork**: Plugin installed to `~/Library/Application Support/Claude/local-agent-mode-plugins/observational-memory/`. Uses the same hook pattern as Claude Code (SessionStart context injection, SessionEnd/UserPromptSubmit/PreCompact checkpoints). Includes a `/recall` command and an `observational-memory` skill. Install with `om install --cowork`.
- **Hermes Agent**: Current OM support includes `om observe --source hermes` and direct transcript parsing. The post-`0.6.0` plan is to make `intertwine/hermes-observational-memory` the first-class Hermes memory-provider plugin, then close the native Hermes PR. Keep the plugin work scoped to the standalone provider repo and avoid broad Hermes core changes unless a missing plugin extension point is proven. See `plans/hermes-first-class-plugin.md` and issue #42.

### API keys

API keys live in `~/.config/observational-memory/env` (created by `om install`, chmod 600). The CLI loads this file on startup via `config.load_env_file()`. The Claude hooks and cron jobs also source it. Environment variables take precedence over the file.

### Prompts

`src/observational_memory/prompts/observer.md` and `src/observational_memory/prompts/reflector.md` define the LLM system prompts. They are bundled as package data. The priority system (🔴/🟡/🟢) and output format are critical — downstream parsing depends on them. The reflector routes observations into three activity sections: `## Active Projects` (software/engineering), `## Life & Operations` (taxes, finance, admin), and `## Creative & Professional` (music, teaching, art). The `startup_memory.py` module extracts all three into `active.md`.
