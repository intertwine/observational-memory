# Observational Memory

[![PyPI version](https://img.shields.io/pypi/v/observational-memory.svg)](https://pypi.org/project/observational-memory/)
[![CI](https://github.com/intertwine/observational-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/intertwine/observational-memory/actions/workflows/ci.yml)

**Cross-agent shared memory for Claude Code and Codex CLI — no RAG, no embeddings, no databases.**

Two background processes (Observer + Reflector) compress your conversation history from multiple AI coding agents into a single shared long-term memory. Every agent reads it on startup and instantly knows about you, your projects, your preferences, and what happened in previous sessions — even sessions with a _different_ agent.

> Adapted from [Mastra's Observational Memory](https://mastra.ai/docs/memory/observational-memory) pattern. See the [OpenClaw version](https://github.com/intertwine/openclaw-observational-memory) for the original.

---

## Why

You use Claude Code in one terminal and Codex CLI in another. Each session starts from scratch — no memory of who you are, what you're working on, or what you told the other agent five minutes ago.

Observational Memory fixes this. A single set of compressed memory files lives at `~/.local/share/observational-memory/` and is shared across all your agents:

<p align="center">
  <img src="assets/system-diagram.webp" alt="Observational Memory system diagram" width="640" />
</p>

### Three tiers of memory

| Tier                | Updated                                              | Retention    | Size                | Contents                        |
| ------------------- | ---------------------------------------------------- | ------------ | ------------------- | ------------------------------- |
| **Raw transcripts** | Real-time                                            | Session only | ~50K tokens/day     | Full conversation               |
| **Observations**    | Per session + periodic checkpoints (~15 min default) | 7 days       | ~2K tokens/day      | Timestamped, prioritized notes  |
| **Reflections**     | Daily                                                | Indefinite   | 200–600 lines total | Identity, projects, preferences |

---

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An API key: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
- Claude Code and/or Codex CLI installed

### Install

```bash
# Option A: Install from PyPI
uv tool install observational-memory

# Option B (macOS): Install from Homebrew tap
brew tap intertwine/tap
brew install intertwine/tap/observational-memory

# Set up hooks, API key, and cron
om install
```

### Verify

```bash
om doctor
```

That's it. Your agents now share persistent, compressed memory.

### Development Install

```bash
git clone https://github.com/intertwine/observational-memory.git
cd observational-memory
uv sync
uv pip install -e ".[dev]"
```

---

## How It Works

### Claude Code Integration

**SessionStart hook** — When you start a Claude Code session, a hook runs `om context` which uses BM25 search to find the most relevant observations and injects them (plus full reflections) as context via `additionalContext`. Falls back to full file dump if search is unavailable.

**SessionEnd hook** — When a session ends, a hook triggers the observer on the just-completed transcript. The observer calls an LLM to compress the conversation into observations.

**UserPromptSubmit / PreCompact hooks** — Long-running sessions also send periodic checkpoint events during the session. These are throttled with `OM_SESSION_OBSERVER_INTERVAL_SECONDS` (default `900` seconds), so observations continue to be captured without observing after every prompt.

To disable in-session checkpoints while keeping normal end-of-session capture, set:
`OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=1` in `~/.config/observational-memory/env`.

All hooks are installed automatically to `~/.claude/settings.json`.

### Codex CLI Integration

**AGENTS.md** — The installer adds instructions to `~/.codex/AGENTS.md` telling Codex to read the memory files at session start.

**Cron observer** — A cron job runs every 15 minutes, scanning `~/.codex/sessions/` for new transcript data (`*.json` and `*.jsonl`) and compressing it into observations.

### Reflector (Both)

A daily cron job (04:00 UTC) runs the reflector, which:

1. Reads the `Last reflected` timestamp from the existing reflections
2. Filters observations to only those from that date onward (incremental — skips already-processed days)
3. If the filtered observations fit in one LLM call (<30K tokens), processes them in a single pass
4. If they're too large (e.g., after a backfill), automatically chunks by date section and folds each chunk into the reflections incrementally
5. Merges, promotes (🟡→🔴), demotes, and archives entries
6. Stamps `Last updated` and `Last reflected` timestamps programmatically
7. Writes the updated `reflections.md`
8. Trims observations older than 7 days

### Priority System

| Level | Meaning                | Examples                                    | Retention  |
| ----- | ---------------------- | ------------------------------------------- | ---------- |
| 🔴    | Important / persistent | User facts, decisions, project architecture | Months+    |
| 🟡    | Contextual             | Current tasks, in-progress work             | Days–weeks |
| 🟢    | Minor / transient      | Greetings, routine checks                   | Hours      |

### LLM Provider & API Keys

The observer and reflector call an LLM API to perform compression. Your API key is stored in a dedicated env file:

```bash
~/.config/observational-memory/env
```

`om install` creates this file with `0600` permissions (owner-read/write only). Edit it to add your key:

```bash
# ~/.config/observational-memory/env
ANTHROPIC_API_KEY=sk-ant-...
```

The CLI, hooks, and cron jobs all source this file automatically — no need to export keys in your shell profile.

- `ANTHROPIC_API_KEY` → uses Claude Sonnet (default)
- `OPENAI_API_KEY` → uses GPT-4o-mini
- Both set → prefers Anthropic
- Environment variables override the env file

---

## CLI Reference

```bash
# Run observer on all recent transcripts
om observe

# Run observer on a specific transcript
om observe --transcript ~/.claude/projects/.../abc123.jsonl

# Run observer for one agent only
om observe --source claude
om observe --source codex

# Run reflector
om reflect

# Search memories
om search "PostgreSQL setup"
om search "current projects" --limit 5
om search "backfill" --json
om search "preferences" --reindex   # rebuild index before searching

# Backfill all historical transcripts
om backfill --source claude
om backfill --dry-run               # preview what would be processed

# Dry run (print output without writing)
om observe --dry-run
om reflect --dry-run

# Install/uninstall
om install [--claude|--codex|--both] [--no-cron]
om uninstall [--claude|--codex|--both] [--purge]

# Check status
om status

# Run diagnostics
om doctor
om doctor --json              # machine-readable output
om doctor --validate-key      # test API key with a live call
```

---

## Configuration

### API Keys

```bash
~/.config/observational-memory/env
```

Created by `om install` with `0600` permissions. Add your key:

```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
# or
OPENAI_API_KEY=sk-...
```

This file is sourced by the `om` CLI, the Claude Code hooks, and the cron jobs. Keys already present in the environment take precedence.

### Memory Location

Default: `~/.local/share/observational-memory/`

Override with `XDG_DATA_HOME`:

```bash
export XDG_DATA_HOME=~/my-data
# Memory will be at ~/my-data/observational-memory/
```

### Cron Schedules

The installer sets up:

- **Observer (Codex):** `*/15 * * * *` by default (controlled by `OM_CODEX_OBSERVER_INTERVAL_MINUTES`, e.g. `*/10 * * * *` for 10 min)
- **Reflector:** `0 4 * * *` (daily at 04:00 UTC)

Set `OM_CODEX_OBSERVER_INTERVAL_MINUTES` in `~/.config/observational-memory/env` to tune Codex polling (`1` = every minute).

Edit with `crontab -e` to adjust.

### Search Backend

Memory search uses a pluggable backend architecture. Three backends are available:

| Backend      | Default | Requires                                     | Method                                                                         |
| ------------ | ------- | -------------------------------------------- | ------------------------------------------------------------------------------ |
| `bm25`       | Yes     | Nothing (bundled)                            | Token-based keyword matching via `rank-bm25`                                   |
| `qmd`        | No      | [QMD CLI](https://github.com/tobi/qmd) + bun | BM25 keyword search via QMD's FTS5 engine                                      |
| `qmd-hybrid` | No      | [QMD CLI](https://github.com/tobi/qmd) + bun | Hybrid BM25 + vector embeddings + LLM reranking (~2GB models, auto-downloaded) |
| `none`       | No      | Nothing                                      | Disables search entirely                                                       |

The default `bm25` backend works out of the box. The index is rebuilt automatically after each observe/reflect run and stored at `~/.local/share/observational-memory/.search-index/bm25.pkl`.

To switch backends, set `OM_SEARCH_BACKEND` in your env file:

```bash
# ~/.config/observational-memory/env
OM_SEARCH_BACKEND=qmd-hybrid
OM_CODEX_OBSERVER_INTERVAL_MINUTES=10
```

Or export it in your shell:

```bash
export OM_SEARCH_BACKEND=qmd-hybrid
export OM_CODEX_OBSERVER_INTERVAL_MINUTES=10
```

#### Using QMD (optional)

[QMD](https://github.com/tobi/qmd) provides hybrid search (BM25 + vector embeddings + LLM reranking) for higher recall on semantic queries. All models run locally via node-llama-cpp — no extra API keys needed. To set it up:

```bash
# 1. Install bun (QMD runtime)
curl -fsSL https://bun.sh/install | bash

# 2. Install QMD (from GitHub — the npm package is a placeholder)
bun install -g github:tobi/qmd

# 3. Switch the backend in config.py
#    search_backend: str = "qmd-hybrid"

# 4. Rebuild the index
om search --reindex "test query"
```

When using QMD, memory documents are written as `.md` files under `~/.local/share/observational-memory/.qmd-docs/` and registered as a QMD collection named `observational-memory`. The `om search` and `om context` commands use whichever backend is configured.

### Tuning

Edit the prompts in `prompts/` to adjust:

- **What gets captured** — priority definitions in `observer.md`
- **How aggressively things are merged** — rules in `reflector.md`
- **Target size** — the reflector aims for 200–600 lines

---

## Example Output

### Observations (`observations.md`)

```markdown
# Observations

## 2026-02-10

### Current Context

- **Active task:** Setting up FastAPI project for task manager app
- **Mood/tone:** Focused, decisive
- **Key entities:** Atlas, FastAPI, PostgreSQL, Tortoise ORM
- **Suggested next:** Help with database models

### Observations

- 🔴 14:00 User is building a task management REST API with FastAPI
- 🔴 14:05 User prefers PostgreSQL over SQLite for production (concurrency)
- 🟡 14:10 Changed mind from SQLAlchemy to Tortoise ORM (finds SQLAlchemy too verbose)
- 🔴 14:15 User's name is Alex, backend engineer, prefers concise code examples
```

### Reflections (`reflections.md`)

```markdown
# Reflections — Long-Term Memory

_Last updated: 2026-02-10 04:00 UTC_
_Last reflected: 2026-02-10_

## Core Identity

- **Name:** Alex
- **Role:** Backend engineer
- **Communication style:** Direct, prefers code over explanation
- **Preferences:** FastAPI, PostgreSQL, Tortoise ORM

## Active Projects

### Task Manager (Atlas)

- **Status:** Active
- **Stack:** Python, FastAPI, PostgreSQL, Tortoise ORM
- **Key decisions:** Postgres for concurrency; Tortoise ORM over SQLAlchemy

## Preferences & Opinions

- 🔴 PostgreSQL over SQLite for production
- 🔴 Concise code examples over long explanations
- 🟡 Tortoise ORM over SQLAlchemy (less verbose)
```

---

## Testing

```bash
# Using make (recommended)
make check          # lint + test
make test           # tests only
make lint           # linter only
make format         # auto-format
make brew-formula   # generate Homebrew formula from current PyPI release
make brew-check     # audit Homebrew formula (requires brew)

# Or directly with uv
uv sync
uv run pytest
uv run pytest tests/test_transcripts.py
uv run pytest -v
```

## Homebrew Release (Maintainers)

`observational-memory` is published to Homebrew via a tap formula (to avoid name collisions with short/common formula names). The executable remains `om`.
The Homebrew release workflow also checks Homebrew/core to catch formula-name collisions before pushing tap updates.

### One-time setup

1. Create tap repo: `intertwine/homebrew-tap`
2. Add repo variable in this repo: `HOMEBREW_TAP_REPO=intertwine/homebrew-tap`
3. Add repo secret in this repo: `HOMEBREW_TAP_GITHUB_TOKEN` (token with push access to the tap repo)

### Per-release flow

1. Publish new version to PyPI.
2. Tag the same version in git (for example `v0.1.2`) and push the tag.
3. GitHub Actions workflow `.github/workflows/homebrew-release.yml` regenerates `packaging/homebrew/observational-memory.rb` from PyPI, updates `Formula/observational-memory.rb` in the tap repo, then commits and pushes the tap update.

### Local maintainership commands

```bash
# Regenerate formula locally
make brew-formula

# Copy into a local tap checkout
make release-homebrew HOMEBREW_TAP_DIR=../homebrew-tap
```

---

## File Structure

```text
observational-memory/
├── README.md                         # This file
├── LICENSE                           # MIT
├── pyproject.toml                    # Python package config
├── src/observational_memory/
│   ├── cli.py                        # CLI: om observe, reflect, search, backfill, install, status
│   ├── config.py                     # Paths, defaults, env detection
│   ├── llm.py                        # LLM API abstraction (Anthropic + OpenAI)
│   ├── observe.py                    # Observer logic
│   ├── reflect.py                    # Reflector logic
│   ├── transcripts/
│   │   ├── claude.py                 # Claude Code JSONL parser
│   │   └── codex.py                  # Codex CLI session parser
│   ├── search/                       # Pluggable search over memory files
│   │   ├── __init__.py               # Document model, factory, reindex orchestrator
│   │   ├── backend.py                # SearchBackend Protocol
│   │   ├── parser.py                 # Parse observations/reflections into Documents
│   │   ├── bm25.py                   # BM25 backend (default, uses rank-bm25)
│   │   ├── qmd.py                    # QMD backend (optional, shells out to qmd CLI)
│   │   └── none.py                   # No-op backend
│   ├── prompts/
│   │   ├── observer.md               # Observer system prompt
│   │   └── reflector.md              # Reflector system prompt
│   └── hooks/claude/
│       ├── session-start.sh          # Inject memory on session start (search-backed)
│       └── session-end.sh            # Trigger observer on session end
└── tests/
    ├── test_transcripts.py           # Transcript parser tests
    ├── test_observe.py               # Observer tests
    ├── test_reflect.py               # Reflector tests
    ├── test_search.py                # Search module tests
    └── fixtures/                     # Sample transcripts
```

---

## How It Compares to the OpenClaw Version

| Feature                | OpenClaw Version        | This Version                                |
| ---------------------- | ----------------------- | ------------------------------------------- |
| **Agents supported**   | OpenClaw only           | Claude Code + Codex CLI                     |
| **Scope**              | Per-workspace           | User-level (shared across all projects)     |
| **Observer trigger**   | OpenClaw cron job       | Claude: SessionEnd hook; Codex: system cron |
| **Context injection**  | AGENTS.md instructions  | Claude: SessionStart hook; Codex: AGENTS.md |
| **Memory location**    | `workspace/memory/`     | `~/.local/share/observational-memory/`      |
| **Compression engine** | OpenClaw agent sessions | Direct LLM API calls (Anthropic/OpenAI)     |
| **Cross-agent memory** | No                      | Yes                                         |

---

## FAQ

**Q: Does this replace RAG / vector search?**
A: For personal context, yes. Observational memory is for remembering _about you_ — preferences, projects, communication style. RAG is for searching document collections. They're complementary. The built-in BM25 search handles keyword retrieval over your memories; for hybrid search (BM25 + vector embeddings + LLM reranking), use the `qmd-hybrid` backend with [QMD](https://github.com/tobi/qmd).

**Q: How much does it cost?**
A: The observer processes only new messages per session (~200–1K input tokens typical). The reflector runs once daily. Expect ~$0.05–0.20/day with Sonnet-class models.

**Q: What if I only use Claude Code?**
A: Run `om install --claude`. The Codex integration is entirely optional.

**Q: Can I manually edit the memory files?**
A: Yes. Both `observations.md` and `reflections.md` are plain markdown. The observer appends; the reflector overwrites. Manual edits to reflections will be preserved.

**Q: What happens if the reflector runs on a huge backlog?**
A: The reflector uses incremental updates — it reads the `Last reflected` timestamp from the existing reflections and only processes new observations since that date. If the timestamp is missing (first run or after a backfill), the reflector automatically chunks observations by date section and folds them incrementally, preventing the model from being overwhelmed. Output token budget is 8192 tokens (enough for the 200–600 line target).

**Q: What about privacy?**
A: Everything runs locally. Transcripts are processed by the LLM API you configure (Anthropic or OpenAI), subject to their data policies. No data is sent anywhere else.

---

## Credits

- Inspired by [Mastra's Observational Memory](https://mastra.ai/docs/memory/observational-memory)
- Original [OpenClaw version](https://github.com/intertwine/openclaw-observational-memory)
- License: MIT
