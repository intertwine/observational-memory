# CLAUDE.md

This file guides Claude Code when working in this repository.

## Current Release Context

The current release line is `v0.8.0`. Do not tag, publish, bump the version, or update Homebrew again unless Bryan explicitly asks for another release.

Current important features:

- v0.8.0 theme — trustworthy memory: durable, provable, conversational (see `docs/RELEASE-0.8.0.md`)
- `om backup` / `om restore`: host-local snapshots, automatic pre-reflect safety snapshot, rotating retention, byte-faithful restore
- `om talk` (experimental): spoken-style conversation grounded in live recall; `OM_TALK_RECALL_TIMEOUT`; recall timeout/unavailable distinguished from "no memory" in every recall consumer; Moss recall backend joins bm25/qmd
- section provenance stamps (`last_reflected`, `derived_from_obs_window`) plus typed owner/scope/source on retrieval objects; inline Markdown stays authoritative
- pluggable scope governance: all share-out paths (cluster snapshots, Moss upload, OM Mail) route through one resolver with `SHAREABLE_SCOPES`; unknown scopes denied by default; `scope=local` never leaves the host
- `om reflect --check-conflicts`: read-only conflict report riding a normal reflect (`--dry-run --check-conflicts` for a pure audit); flags silently-changed high-stakes facts
- memory-growth instrumentation (B0) in `om doctor` and `om context --quality-report`: per-document/per-section sizes and coldness from real stamps only, never a guess
- OM Mail (experimental, CLI-only, off by default): email inboxes as a memory substrate — Ed25519-signed envelopes, locally pinned peers, encrypted context packs, recall negotiation, held-quarantine fail-closed; `agentmail` + `localdir` providers; validated live across two machines (`docs/mail-memory.md`)
- public plugin seams: `observational_memory.mail_providers` and `observational_memory.cli_plugins` entry points (built-ins/core win collisions; broken plugins fail loud, not silent); contributor terms in `CONTRIBUTING.md`
- async-Batch pre-submit model guard: cross-provider reflector models rejected before the Batch job is created
- hermetic-by-default test suite: ambient `OM_*` and provider env stripped per test
- section-targeted reflection for scale (`OM_REFLECTOR_STRATEGY=legacy|sectioned|auto`, default `auto`): routes observations to impacted sections, patches only touched sections, reassembles byte-for-byte, fails closed on invalid model output
- fail-closed startup hooks (Claude/Grok/Cowork `SessionStart` route through bounded `om context` only; never dump raw memory files on failure)
- reflector budgets and output caps (`OM_REFLECTOR_MAX_INPUT_TOKENS`, `OM_REFLECTOR_OBSERVATION_CHUNK_RATIO`, `OM_REFLECTOR_OUTPUT_MAX_CHARS`)
- host-local usage/cost tracking and budgets with `om usage`; offline reflection via the API-key OpenAI Batch API (`om reflect --async`, `om jobs list|poll|show|cancel`)
- startup-context quality controls (cross-section dedup, operational-fact freshness markers, cwd/task scope, `om context --quality-report`)
- `om login` for OpenAI ChatGPT and xAI SuperGrok subscriptions (host-local token store, Codex Responses-API routing, per-workflow provider selection)
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

Observational Memory is user-level memory for Claude Code, Codex, OpenCode, Grok Build TUI, Cowork, and Hermes.

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
- `src/observational_memory/transcripts/`: Claude, Codex, OpenCode, Grok, Cowork, Hermes, and Claude auto-memory parsers.

## Agent Integrations

- Claude Code: hooks for startup context, session end, prompt submit, and pre-compact checkpoints.
- Codex: hooks-first startup and Stop checkpoints, plus a conditional AGENTS fallback.
- OpenCode: local plugin event capture plus a global AGENTS fallback.
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
- `docs/talk-to-memories.md`: `om talk` conversation and recall backends.
- `docs/configuration.md`: env vars, paths, providers, schedules.
- `docs/om-cluster-sync.md`: OM Cluster operations.
- `docs/mail-memory.md`: OM Mail — email inboxes as a memory substrate (experimental).
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

`v0.8.0` has release notes in `docs/RELEASE-0.8.0.md`. Future release steps require explicit user approval.
