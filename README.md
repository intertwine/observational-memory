# Observational Memory

**Cross-agent shared memory for Claude Code and Codex CLI — no RAG, no embeddings, no databases.**

Two background processes (Observer + Reflector) compress your conversation history from multiple AI coding agents into a single shared long-term memory. Every agent reads it on startup and instantly knows about you, your projects, your preferences, and what happened in previous sessions — even sessions with a *different* agent.

> Adapted from [Mastra's Observational Memory](https://mastra.ai/docs/memory/observational-memory) pattern. See the [OpenClaw version](https://github.com/intertwine/openclaw-observational-memory) for the original.

---

## Why

You use Claude Code in one terminal and Codex CLI in another. Each session starts from scratch — no memory of who you are, what you're working on, or what you told the other agent five minutes ago.

Observational Memory fixes this. A single set of compressed memory files lives at `~/.local/share/observational-memory/` and is shared across all your agents:

```
  Claude Code session              Codex CLI session
  ┌──────────────────────┐        ┌──────────────────────┐
  │ SessionStart hook     │        │ AGENTS.md reads       │
  │ → injects memory      │        │ → memory on startup   │
  │                       │        │                       │
  │ SessionEnd hook       │        │ Cron-based observer   │
  │ → triggers observer   │        │ → scans sessions      │
  └───────────┬───────────┘        └───────────┬───────────┘
              │ transcript                      │ transcript
              ▼                                 ▼
  ┌─────────────────────────────────────────────────────┐
  │              observe.py (LLM compression)           │
  └──────────────────────┬──────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────┐
  │ ~/.local/share/observational-memory/                │
  │   observations.md   — recent compressed notes       │
  │   reflections.md    — stable long-term memory       │
  └──────────────────────┬──────────────────────────────┘
                         ▼
  ┌─────────────────────────────────────────────────────┐
  │         reflect.py (daily consolidation)            │
  └─────────────────────────────────────────────────────┘
```

### Three tiers of memory

| Tier | Updated | Retention | Size | Contents |
|------|---------|-----------|------|----------|
| **Raw transcripts** | Real-time | Session only | ~50K tokens/day | Full conversation |
| **Observations** | Per session + every 15 min | 7 days | ~2K tokens/day | Timestamped, prioritized notes |
| **Reflections** | Daily | Indefinite | 200–600 lines total | Identity, projects, preferences |

---

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An API key: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
- Claude Code and/or Codex CLI installed

### Install

```bash
# Clone and install
git clone https://github.com/intertwine/observational-memory.git
cd observational-memory
uv tool install .

# Or with pip
pip install .
```

### Set up for both agents

```bash
# Install hooks, AGENTS.md additions, and cron jobs
om install --both

# Or just one agent
om install --claude
om install --codex
```

### Verify

```bash
om status
```

That's it. Your agents now share persistent, compressed memory.

---

## How It Works

### Claude Code Integration

**SessionStart hook** — When you start a Claude Code session, a hook reads `reflections.md` and `observations.md` and injects them as context via `additionalContext`. Claude instantly has your full memory.

**SessionEnd hook** — When a session ends, a hook triggers the observer on the just-completed transcript. The observer calls an LLM to compress the conversation into observations.

Both hooks are installed automatically to `~/.claude/settings.json`.

### Codex CLI Integration

**AGENTS.md** — The installer adds instructions to `~/.codex/AGENTS.md` telling Codex to read the memory files at session start.

**Cron observer** — A cron job runs every 15 minutes, scanning `~/.codex/sessions/` for new transcript data and compressing it into observations.

### Reflector (Both)

A daily cron job (04:00 UTC) runs the reflector, which:
1. Reads all observations + existing reflections
2. Merges, promotes (🟡→🔴), demotes, and archives entries
3. Writes a clean `reflections.md`
4. Trims observations older than 7 days

### Priority System

| Level | Meaning | Examples | Retention |
|-------|---------|----------|-----------|
| 🔴 | Important / persistent | User facts, decisions, project architecture | Months+ |
| 🟡 | Contextual | Current tasks, in-progress work | Days–weeks |
| 🟢 | Minor / transient | Greetings, routine checks | Hours |

### LLM Provider & API Keys

The observer and reflector call an LLM API to perform compression. Your API key is stored in a dedicated env file:

```
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

# Dry run (print output without writing)
om observe --dry-run
om reflect --dry-run

# Install/uninstall
om install [--claude|--codex|--both] [--no-cron]
om uninstall [--claude|--codex|--both] [--purge]

# Check status
om status
```

---

## Configuration

### API Keys

```
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
- **Observer (Codex):** `*/15 * * * *` (every 15 min)
- **Reflector:** `0 4 * * *` (daily at 04:00 UTC)

Edit with `crontab -e` to adjust.

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

*Last updated: 2026-02-10 04:00 UTC*

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
# Install dev dependencies
uv sync

# Run tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_transcripts.py

# Verbose output
uv run pytest -v
```

---

## File Structure

```
observational-memory/
├── README.md                         # This file
├── LICENSE                           # MIT
├── pyproject.toml                    # Python package config
├── src/observational_memory/
│   ├── cli.py                        # CLI: om observe, om reflect, om install, om status
│   ├── config.py                     # Paths, defaults, env detection
│   ├── llm.py                        # LLM API abstraction (Anthropic + OpenAI)
│   ├── observe.py                    # Observer logic
│   ├── reflect.py                    # Reflector logic
│   ├── transcripts/
│   │   ├── claude.py                 # Claude Code JSONL parser
│   │   └── codex.py                  # Codex CLI session parser
│   ├── prompts/
│   │   ├── observer.md               # Observer system prompt
│   │   └── reflector.md              # Reflector system prompt
│   └── hooks/claude/
│       ├── session-start.sh          # Inject memory on session start
│       └── session-end.sh            # Trigger observer on session end
└── tests/
    ├── test_transcripts.py           # Transcript parser tests
    ├── test_observe.py               # Observer tests
    ├── test_reflect.py               # Reflector tests
    └── fixtures/                     # Sample transcripts
```

---

## How It Compares to the OpenClaw Version

| Feature | OpenClaw Version | This Version |
|---------|-----------------|--------------|
| **Agents supported** | OpenClaw only | Claude Code + Codex CLI |
| **Scope** | Per-workspace | User-level (shared across all projects) |
| **Observer trigger** | OpenClaw cron job | Claude: SessionEnd hook; Codex: system cron |
| **Context injection** | AGENTS.md instructions | Claude: SessionStart hook; Codex: AGENTS.md |
| **Memory location** | `workspace/memory/` | `~/.local/share/observational-memory/` |
| **Compression engine** | OpenClaw agent sessions | Direct LLM API calls (Anthropic/OpenAI) |
| **Cross-agent memory** | No | Yes |

---

## FAQ

**Q: Does this replace RAG / vector search?**
A: For personal context, yes. Observational memory is for remembering *about you* — preferences, projects, communication style. RAG is for searching document collections. They're complementary.

**Q: How much does it cost?**
A: The observer processes only new messages per session (~200–1K input tokens typical). The reflector runs once daily. Expect ~$0.05–0.20/day with Sonnet-class models.

**Q: What if I only use Claude Code?**
A: Run `om install --claude`. The Codex integration is entirely optional.

**Q: Can I manually edit the memory files?**
A: Yes. Both `observations.md` and `reflections.md` are plain markdown. The observer appends; the reflector overwrites. Manual edits to reflections will be preserved.

**Q: What about privacy?**
A: Everything runs locally. Transcripts are processed by the LLM API you configure (Anthropic or OpenAI), subject to their data policies. No data is sent anywhere else.

---

## Credits

- Inspired by [Mastra's Observational Memory](https://mastra.ai/docs/memory/observational-memory)
- Original [OpenClaw version](https://github.com/intertwine/openclaw-observational-memory)
- License: MIT
