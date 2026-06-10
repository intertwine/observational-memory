# Observational Memory

![Observational Memory header showing local agent memory moving through Codex hooks, Claude, Grok, ChatGPT Memory, Claude Cowork, and Hermes.](assets/observational-memory-header.png)

[![PyPI version](https://img.shields.io/pypi/v/observational-memory.svg)](https://pypi.org/project/observational-memory/)
[![PyPI downloads](https://img.shields.io/pypi/dm/observational-memory.svg)](https://pypi.org/project/observational-memory/)
[![CI](https://github.com/intertwine/observational-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/intertwine/observational-memory/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/intertwine/observational-memory?style=social)](https://github.com/intertwine/observational-memory/stargazers)

**Local memory for the agents you already use.**

Observational Memory, or `om`, gives Claude Code, Codex, Grok Build TUI, Claude Cowork, and Hermes one shared memory on your machine. It watches agent transcripts, writes useful notes into local Markdown files, and gives new sessions a compact startup context. You can search that memory later, export reviewed memory bundles for hosted platforms, or opt in to encrypted multi-machine sync with OM Cluster.

The current release is `v0.7.0`. It includes:

- **Reflection that scales** — section-targeted reflection (`OM_REFLECTOR_STRATEGY=auto`) routes observations to the sections they affect, keeps a core bundle in every fold, patches only touched sections, and reassembles the rest byte-for-byte — so a growing `reflections.md` no longer forces whole-document resend; invalid model output fails closed, leaving memory unchanged
- **Fail-closed startup context** — the Claude, Grok, and Cowork hooks route through bounded `om context` only; if it's unavailable they emit nothing (plus a stderr hint) instead of dumping raw, unbounded memory files
- **Configurable, honest reflector budgets** — `OM_REFLECTOR_MAX_INPUT_TOKENS` / `OM_REFLECTOR_OBSERVATION_CHUNK_RATIO` knobs, a default that no longer silently clamps your configured cap, and diagnostics that report configured-vs-effective limits
- **Codex-safe reflector output cap** — `OM_REFLECTOR_OUTPUT_MAX_CHARS` bounds the emitted document on every backend (trimming at a section boundary), even the ChatGPT Codex path that rejects `max_output_tokens`
- **Clean async-Batch errors** — `om reflect --async` reports billing/quota failures as a one-line message, not a raw traceback
- **See and cap LLM spend** — every observe/reflect call records tokens and an estimated cost, with token/dollar budgets (hard or soft, per day/month/session) that stop a runaway job before it bills (`om usage status`, `om usage budget`)
- **Offline reflection via OpenAI Batch** — `om reflect --async` submits a job and `om jobs poll` applies it later, at ~50% token cost on a metered OpenAI key
- **Cheaper, faster observe/reflect** — bounded reflector input, ChatGPT Codex reasoning-effort control, and Anthropic prompt caching
- **Higher-quality startup context** — cross-section de-duplication, freshness markers on stale operational facts, and cwd/task-aware scope, inspectable with `om context --quality-report`
- **`om login` for your ChatGPT or SuperGrok subscription** so the observer and reflector run off your existing plan instead of a metered API key
- first-class Grok Build TUI hooks and transcript observation
- budgeted startup context through `om context`, with compact profile projection and project-level active-context routing
- first-class recall through `om recall`
- richer reflection metadata and host-memory controls
- OM Cluster relay operations, health checks, and public-safe validation docs
- Windows, macOS, and Linux install paths

For configuration of all of these — providers, usage budgets, async Batch, and startup quality — see [`docs/configuration.md`](docs/configuration.md). Subscription-auth background is in [`docs/RELEASE-0.6.5.md`](docs/RELEASE-0.6.5.md).

## Quick Install

macOS with Homebrew:

```bash
brew install intertwine/tap/observational-memory
om install
om doctor
```

Linux, macOS, or Windows with `uv`:

```bash
uv tool install observational-memory
om install
om doctor
```

Install the optional enterprise auth extras if you use Anthropic through Vertex AI or Bedrock:

```bash
uv tool install "observational-memory[enterprise]"
```

## What It Does

`om` keeps four main memory files under your local data directory:

| File | Purpose |
| --- | --- |
| `observations.md` | Recent notes from sessions and checkpoints. |
| `reflections.md` | Longer-term facts, preferences, decisions, and active work. |
| `profile.md` | Compact stable context for startup. |
| `active.md` | Compact current context for startup. |

Those files are plain Markdown. You can read them, back them up, and search them.

Default paths:

| Platform | Memory directory | Config directory |
| --- | --- | --- |
| macOS / Linux | `~/.local/share/observational-memory/` | `~/.config/observational-memory/` |
| Windows | `%LOCALAPPDATA%\observational-memory\` | `%APPDATA%\observational-memory\` |

## How Memory Flows

```mermaid
flowchart LR
    A["Claude Code, Codex, Grok, Cowork, Hermes logs"] --> B["om observe"]
    C["Claude auto-memory files"] --> D["search index"]
    B --> E["observations.md"]
    E --> F["om reflect"]
    D --> F
    F --> G["reflections.md"]
    G --> H["profile.md + active.md"]
    H --> I["om context startup pack"]
    G --> J["om recall / om search"]
```

## First Week Workflow

1. Install `om`.
2. Run `om install` and answer the provider questions.
3. Run `om doctor`.
4. Start using Claude Code, Codex, or Grok normally.
5. Search memory when you need it:

```bash
om recall --query "current project status"
om search "release checklist"
```

6. Check generated startup context:

```bash
om context --for codex --cwd "$PWD" --task "finish docs"
```

## Guides

Start here:

- [Documentation index](docs/README.md)
- [Install and setup](docs/install.md)
- [Platform integrations](docs/integrations.md)
- [Hermes plugin](docs/hermes-plugin.md)
- [Search, recall, and startup context](docs/search-and-recall.md)
- [Talk to your memories (`om talk`)](docs/talk-to-memories.md)
- [Configuration](docs/configuration.md)
- [OM Cluster sync](docs/om-cluster-sync.md)
- [OM Mail: email inboxes as a memory substrate (experimental)](docs/mail-memory.md)
- [OM Cluster validation checklist](docs/om-cluster-validation.md)
- [Host memory coexistence](docs/coexistence.md)
- [Maintainer guide](docs/MAINTAINERS.md)

## Agent Support

| Host | Current support |
| --- | --- |
| Claude Code | Hooks for startup context and checkpoints. |
| Codex | Hooks-first startup and Stop checkpoints, with an AGENTS fallback. |
| Grok Build TUI | Native hook file with Claude-compatibility awareness, plus `updates.jsonl` observation. |
| Claude Cowork | Local plugin on macOS with hooks and `/recall`. |
| Hermes | External memory-provider plugin through [intertwine/hermes-observational-memory](https://github.com/intertwine/hermes-observational-memory), plus manual session-log ingestion. |
| ChatGPT / Claude Managed Agents | Reviewed export bundles through `om export`; `om` does not silently write hosted memory. |

## Common Commands

```bash
om status
om doctor
om observe --source codex
om reflect
om reflect --async              # submit an offline OpenAI Batch job (API-key 'openai')
om jobs poll                    # apply completed async jobs
om recall --query "what was decided about sync?"
om recall --handle startup:active
om search "preferences" --json
om usage status                 # token usage, cost, and budgets
om usage budget set --daily-usd 5.00
om context --quality-report     # startup-context dedup / freshness / budget report
om export --target chatgpt
om export --target claude-managed-agents --output ./om-claude-memory
```

OM Cluster is off until you initialize or join a cluster:

```bash
om cluster init --name "Personal Memory" --transport filesystem:~/Sync/om-cluster --import-existing
om cluster invite --expires 10m
om cluster join "omc1:..."
om cluster requests
om cluster approve join_...
om cluster sync
om cluster status
```

Do not sync `~/.local/share/observational-memory/` directly with Dropbox, iCloud, Syncthing, rsync, or a NAS. Use the cluster transport directory instead.

## Architecture At A Glance

<p align="center">
  <img src="assets/system-diagram.jpeg" alt="Observational Memory system diagram showing agent hooks feeding shared local markdown memory, search, and reflection." width="980" />
</p>

The short version:

- `om observe` turns transcripts into recent notes.
- `om reflect` turns recent notes into durable memory.
- `om context` gives agents a bounded startup pack.
- `om recall` and `om search` retrieve more when the startup pack is not enough.
- `om export` prepares reviewed memory seed bundles for hosted systems.
- `om cluster` syncs encrypted records across machines when you opt in.

## Release State

`v0.7.0` is the current release. It makes reflection scale with **section-targeted reflection** (`OM_REFLECTOR_STRATEGY=legacy|sectioned|auto`, default `auto`): observations route to the sections they affect, a core bundle rides every fold, only touched sections are patched, and the rest is reassembled byte-for-byte — ending the O(chunks×size) whole-document resend at 10x/100x scale, with invalid model output failing closed. It builds on the v0.6.7 reflector budget knobs + output cap and fail-closed startup hooks, the v0.6.6 usage/cost/budget subsystem (`om usage`) and OpenAI Batch async reflection (`om reflect --async`). The addressable memory-unit store and hierarchical compaction are deferred to v0.8.0+ (issue #71).

Before the next release, maintainers should run:

```bash
make check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

See [docs/MAINTAINERS.md](docs/MAINTAINERS.md) for the full release workflow.
