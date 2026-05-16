# Release 0.6.3

Released May 16, 2026.

## Summary

`v0.6.3` makes Grok Build TUI a first-class local agent in Observational Memory. Grok can now receive startup context through OM-managed hooks, contribute session observations from `updates.jsonl`, and show up in `om status` and `om doctor` alongside Claude Code, Codex, Cowork, and Hermes.

## User-Facing Changes

- `om install --grok` installs an OM-owned Grok hook file at `~/.grok/hooks/observational-memory.json`.
- `om install --all` now includes Grok support.
- `om uninstall --grok` removes the OM Grok hook file.
- Grok startup context avoids duplicate injection when existing Claude Code OM hooks are already available through Grok's Claude compatibility layer.
- `om observe --source grok` scans recent Grok sessions.
- `om grok-checkpoint --transcript <updates.jsonl>` supports hook-driven checkpoints.
- `om status` and `om doctor` report Grok hook and native-memory state.

## Compatibility Notes

- Claude Code, Codex, and Grok now have installer-managed hooks.
- Cowork remains a macOS local plugin.
- Hermes remains transcript ingestion only; the first-class Hermes plugin is still planned separately.
- Grok's native memory is independent of OM memory. Use OM when you want shared local memory across agents.
- OM Cluster remains opt-in and unchanged.

## Validation

Release validation:

```bash
make check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Recommended Grok checks:

```bash
om install --grok --non-interactive
om doctor
om observe --source grok --dry-run
om grok-checkpoint --transcript ~/.grok/sessions/.../updates.jsonl
```

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.6.3
om install --all --non-interactive
om doctor
```

For maintainer release steps, see [MAINTAINERS.md](MAINTAINERS.md).
