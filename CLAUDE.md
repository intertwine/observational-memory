# CLAUDE.md

This file guides Claude Code when working in this repository.

## Current Release Context

The current release line is `v0.7.0`. Do not tag, publish, bump the version, or update Homebrew again unless Bryan explicitly asks for another release.

Current important features:

- section-targeted reflection for scale (`OM_REFLECTOR_STRATEGY=legacy|sectioned|auto`, default `auto`): routes observations to impacted sections, always includes a core bundle, patches only touched sections, reassembles byte-for-byte, fails closed on invalid model output — ends the O(chunks×size) whole-document resend at 10x/100x
- fail-closed startup hooks (Claude/Grok/Cowork `SessionStart` route through bounded `om context` only; never dump raw `profile.md`/`active.md`/`reflections.md`/`observations.md` on failure)
- configurable, honestly-diagnosed reflector input budget (`OM_REFLECTOR_MAX_INPUT_TOKENS`, `OM_REFLECTOR_OBSERVATION_CHUNK_RATIO`; configured-vs-effective cap reporting)
- Codex-safe reflector output cap (`OM_REFLECTOR_OUTPUT_MAX_CHARS`, section-boundary trim, applied post-call so it bounds the `openai-chatgpt` path too)
- clean async-Batch error UX (`om reflect --async` reports billing/quota failures as one-line CLI errors, not tracebacks)
- host-local usage/cost tracking and budgets with `om usage` (SQLite `usage.sqlite`, shipped pricing snapshot, hard/soft token & dollar budgets, `om doctor` integration)
- offline reflection via the API-key OpenAI Batch API (`om reflect --async`, `om jobs list|poll|show|cancel`, `OM_OPENAI_ASYNC_MODE`)
- observe/reflect cost & latency controls (bounded reflector input via `OM_REFLECTOR_CONTEXT_MAX_CHARS`, Codex reasoning effort via `OM_OPENAI_CHATGPT_REASONING_EFFORT`, Anthropic prompt caching)
- startup-context quality controls (cross-section dedup, operational-fact freshness markers, cwd/task scope, `om context --quality-report`)
- `om login` for OpenAI ChatGPT and xAI SuperGrok subscriptions (host-local token store, Codex Responses-API routing, per-workflow provider selection)
- budgeted startup context with `om context`
- first-class recall with `om recall`
- first-class Grok Build TUI hooks and transcript observation
- richer reflection metadata and host-local scope controls
- opt-in OM Cluster sync, stdlib relay server (`om-relay`, `om cluster relay serve`), relay health checks, and public-safe cluster validation docs

## Build And Test

Use `uv` through the repo toolchain.

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Preferred Makefile targets:

```bash
make check
make test
make lint
make format
make build
make clean
make install-dev
make doctor
```

CI runs `ruff check`, `ruff format --check`, and `pytest` on Python 3.11, 3.12, and 3.13. A local lint pass must include the format check.

## Architecture

Observational Memory is user-level memory for Claude Code, Codex, Grok Build TUI, Cowork, and Hermes.

```text
transcripts -> om observe -> observations.md
observations.md + auto-memory -> om reflect -> reflections.md
reflections.md -> profile.md + active.md -> om context
reflections/search index -> om recall / om search
cluster records -> materialized Markdown views
```

Important modules:

- `src/observational_memory/cli.py`: Click CLI.
- `src/observational_memory/auth/`: subscription OAuth (`om login` for ChatGPT/xAI), host-local token store, runtime credential resolution.
- `src/observational_memory/observe.py`: transcript observation.
- `src/observational_memory/reflect.py`: durable reflection.
- `src/observational_memory/startup_memory.py`: budgeted startup packs and recall handles.
- `src/observational_memory/reflection_metadata.py`: inline metadata, local scope, and conflict detection.
- `src/observational_memory/usage/`: host-local LLM usage tracking, cost estimation, and budget enforcement (`usage.sqlite`, never synced).
- `src/observational_memory/jobs/`: async provider jobs — API-key OpenAI Batch backend for `om reflect --async` (host-local job store under `.provider-jobs/`, never synced; never used for `openai-chatgpt`).
- `src/observational_memory/search/`: BM25, QMD, and no-op search backends.
- `src/observational_memory/sync/`: OM Cluster config, records, crypto, materialization, and transports.
- `src/observational_memory/sync/relay_server.py`: supported stdlib relay server.
- `src/observational_memory/transcripts/`: Claude, Codex, Grok, Cowork, Hermes, and Claude auto-memory parsers.

## Agent Integrations

- Claude Code: hooks for startup context, session end, prompt submit, and pre-compact checkpoints.
- Codex: hooks-first startup and Stop checkpoints, plus a conditional AGENTS fallback.
- Grok Build TUI: native hook file with Claude-compatibility awareness, plus `updates.jsonl` observation.
- Cowork: macOS local plugin with hooks and `/recall`.
- Hermes: core `om` supports manual transcript ingestion; live startup context, search, explicit writes, and OM Cluster participation come from the external `intertwine/hermes-observational-memory` Hermes memory-provider plugin.

Do not document Hermes as `om install` hook-installed. The Hermes plugin is installed and selected through Hermes itself.

## Documentation Rules

Keep the README short. Put deeper material in `docs/`.

Current docs:

- `README.md`: short user doorway.
- `docs/install.md`: user install guide.
- `docs/integrations.md`: platform integrations.
- `docs/search-and-recall.md`: startup, recall, search, and QMD basics.
- `docs/configuration.md`: env vars, paths, providers, schedules.
- `docs/om-cluster-sync.md`: OM Cluster operations.
- `docs/om-cluster-validation.md`: public-safe cluster validation.
- `docs/MAINTAINERS.md`: maintainer workflows.

Use plain English. Aim for a 10th grade reading level. Prefer short sections and tested CLI snippets.

Archive completed plans and old status reports under `docs/archive/`. Keep active plans in `plans/`.

## OM Cluster Rules

- Cluster sync is opt-in.
- Do not sync `~/.local/share/observational-memory/` directly.
- Use a transport directory or relay endpoint.
- Treat filesystem, relay, and P2P transports as untrusted.
- Relay access is not cluster trust.
- Do not print secrets, provider keys, node private keys, request secrets, `data_keys`, or real private memory in public docs.
- `scope=local` reflection entries must not become shared cluster memory.

## Validation Shortcuts

Full local check:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Focused cluster check:

```bash
uv run pytest tests/sync/test_filesystem_sync.py tests/sync/test_relay_transport.py tests/sync/test_store_and_materialize.py
```

Startup/recall smoke check:

```bash
OM_CLUSTER_ENABLED=0 uv run om context >/tmp/om-context.json
uv run om recall --query "current work" --limit 3
```

## Release Boundary

`v0.7.0` has release notes in `docs/RELEASE-0.7.0.md`. Future release steps require explicit user approval.
