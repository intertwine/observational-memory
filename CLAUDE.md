# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test

```bash
uv sync                          # install deps
uv run pytest                    # run all tests
uv run pytest tests/test_transcripts.py  # single test file
uv run pytest -v                 # verbose
uv run om status                 # check local installation status
```

## Architecture

Cross-agent observational memory that works at the **user level** (not per-project) across Claude Code and Codex CLI. Two background processes compress conversation transcripts into shared memory files at `~/.local/share/observational-memory/`.

### Data flow

```
Transcripts (Claude JSONL / Codex sessions)
  → observe.py (LLM compression → observations.md)
  → reflect.py (daily consolidation → reflections.md)
```

### Key modules

- **`src/observational_memory/transcripts/claude.py`** — Parses Claude Code `.jsonl` transcripts. Each line is a JSON object with `type` (user/assistant/progress), `message.content` (text or array of blocks), `uuid`, `timestamp`.
- **`src/observational_memory/transcripts/codex.py`** — Parses Codex CLI session files from `~/.codex/sessions/`.
- **`src/observational_memory/observe.py`** — Observer: reads transcripts, finds new messages via cursor bookmarks, calls LLM to compress, appends to `observations.md`.
- **`src/observational_memory/reflect.py`** — Reflector: reads observations + reflections, calls LLM to condense, writes `reflections.md`, trims old observations.
- **`src/observational_memory/llm.py`** — Thin abstraction over Anthropic and OpenAI APIs. Auto-detects provider from env vars.
- **`src/observational_memory/config.py`** — All paths, defaults, cursor management. Memory dir follows XDG spec. Search backend is configurable via `OM_SEARCH_BACKEND` env var.
- **`src/observational_memory/cli.py`** — Click CLI (`om` command). Commands: observe, reflect, backfill, search, context, install, uninstall, status.
- **`src/observational_memory/search/`** — Pluggable search over memory files. BM25 backend (default, uses `rank-bm25`), QMD backend (optional, shells out to `qmd` CLI — `"qmd"` for keyword search, `"qmd-hybrid"` for hybrid BM25 + vector + LLM reranking), None backend (no-op). Parser splits observations by date, reflections by section. The `reindex()` orchestrator is called automatically after observe/reflect writes.

### Agent integration

- **Claude Code**: `SessionStart` hook injects memory via `additionalContext`; `SessionEnd` hook triggers observer. Hooks live in `src/observational_memory/hooks/claude/` (bundled as package data) and are registered in `~/.claude/settings.json`.
- **Codex CLI**: Instructions appended to `~/.codex/AGENTS.md`; cron job for observer.

### API keys

API keys live in `~/.config/observational-memory/env` (created by `om install`, chmod 600). The CLI loads this file on startup via `config.load_env_file()`. The Claude hooks and cron jobs also source it. Environment variables take precedence over the file.

### Prompts

`src/observational_memory/prompts/observer.md` and `src/observational_memory/prompts/reflector.md` define the LLM system prompts. They are bundled as package data. The priority system (🔴/🟡/🟢) and output format are critical — downstream parsing depends on them.
