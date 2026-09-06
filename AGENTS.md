# AGENTS.md

This repo uses Observational Memory itself, but repo work should remain local-first and reviewable.

## Current Goal Shape

The final authorized release is `v0.10.1`. This project is unmaintained legacy
software; see `docs/legacy-migration.md`. Historical plans are not authority to
restart development, install hooks, or publish another version.

## Work Rules

- Confirm the repo state with `git status --short --branch` before edits.
- Preserve unrelated user changes.
- Use `uv run` for Python commands.
- Run the exact CI lint command before pushing:

```bash
uv run ruff check .
uv run ruff format --check .
```

- Run tests appropriate to the change. For broad changes, run:

```bash
uv run pytest
```

## Documentation Rules

- Keep `README.md` short and user-facing.
- Put deeper guides in `docs/`.
- Archive completed implementation plans and old status reports under `docs/archive/`.
- Keep active plans in `plans/`.
- Use plain English at about a 10th grade reading level.
- Include working CLI snippets when a feature has a CLI path.
- Do not include private hostnames, IPs, tunnels, provider keys, node private keys, request secrets, `data_keys`, or real private memory.

## Important Docs

- `docs/install.md`
- `docs/integrations.md`
- `docs/search-and-recall.md`
- `docs/configuration.md`
- `docs/om-cluster-sync.md`
- `docs/om-cluster-validation.md`
- `docs/MAINTAINERS.md`

## Current Feature Boundaries

- Claude Code, Codex, and Grok have installer-managed hooks.
- Cowork has a macOS local plugin.
- Hermes is transcript ingestion only in this repo today.
- Do not document Hermes as `om install` hook-installed. The Hermes plugin is installed and selected through Hermes itself.
- The v0.10 native bridge is macOS-only and covers Claude Code and Codex native memory. Bridge activation removes OM-managed Claude/Codex/Cowork writer hooks while preserving read-only SessionStart context; Grok, Kimi, and OpenCode are outside that migration scope. OM v0.10 requires Hermes memory-provider plugin v1.5.1+ and Grok marketplace plugin v0.1.2+ on hosts that use those separately released plugins.
- OM Cluster is opt-in and disabled unless initialized or joined.
- Relay transport is supported, but relay access is not cluster trust.
- Do not sync `~/.local/share/observational-memory/` directly; use a transport directory or relay endpoint.
- Treat filesystem, relay, and P2P transports as untrusted.
- `scope=local` reflection entries must not become shared cluster memory.
- Hosted memory exports are review bundles; `om` does not silently write ChatGPT or Claude Managed Agents memory.
- Usage tracking, cost, and budgets (`om usage`) are host-local in `usage.sqlite`; never synced via OM Cluster.
- OpenAI Batch async reflection (`om reflect --async`, `om jobs`) is API-key `openai` only and never selected for the `openai-chatgpt` subscription provider.
- Startup-context quality passes (dedup, freshness, scope, `om context --quality-report`) operate on the budgeted payload only; `om recall` still returns the full sections.
