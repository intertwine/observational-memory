# Release 0.6.2

Released May 15, 2026.

## Summary

`v0.6.2` tunes startup context for large, long-running Observational Memory corpora. Startup remains small and useful, while full generated memory stays available through `om recall`.

## User-Facing Changes

- `om context` now projects oversized stable profile sections into a compact `Working Profile`.
- Startup output strips inline OM provenance comments to save budget. Full provenance remains in generated Markdown and recall output.
- Active context can be split by project-level subsections so one large active file does not crowd out useful projects.
- Projected startup handles can be expanded through `om recall`.
- Empty startup chunks are skipped so placeholder sections do not consume budget.

## Compatibility Notes

- `profile.md`, `active.md`, `reflections.md`, and `observations.md` remain the readable generated memory views.
- `om recall --handle startup:profile` and `om recall --handle startup:active` still return full generated startup files.
- Cluster sync remains opt-in and unchanged.

## Validation

Release validation:

```bash
make check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Recommended startup checks:

```bash
OM_CLUSTER_ENABLED=0 om context --for codex --cwd "$PWD" --task "startup payload shape"
om recall --handle startup:profile
om recall --query "current work" --limit 3
```

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.6.2
om install
om doctor
```

For maintainer release steps, see [MAINTAINERS.md](MAINTAINERS.md).
