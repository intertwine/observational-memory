# AGENTS.md

This repo uses Observational Memory itself, but repo work should remain local-first and reviewable.

## Current Goal Shape

The next release target is `v0.6.1`. Documentation and hardening can be prepared now. Do not bump versions, tag releases, publish to PyPI, or update Homebrew unless Bryan explicitly asks.

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

- Claude Code and Codex have installer-managed hooks.
- Cowork has a macOS local plugin.
- Hermes is transcript ingestion only in this repo today.
- OM Cluster is opt-in and disabled unless initialized or joined.
- Relay transport is supported, but relay access is not cluster trust.
- Hosted memory exports are review bundles; `om` does not silently write ChatGPT or Claude Managed Agents memory.
