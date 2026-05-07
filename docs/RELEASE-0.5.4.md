# Release 0.5.4

This branch is prepared for publishing `observational-memory` `0.5.4`.

## What's in 0.5.4

- Adds Claude Cowork transcript ingestion via `om observe --source cowork` and `om backfill --source cowork`.
- Bundles a local Cowork plugin with SessionStart memory injection, checkpoint hooks, `/recall`, and an `observational-memory` skill.
- Updates the Cowork plugin hook config to the current Claude plugin schema and adds plugin version metadata for resync.
- Extends `om install`, `om uninstall`, `om status`, and `om doctor` with Cowork plugin support.
- Improves observer/reflector resilience with provider inference for per-operation model overrides and retry handling for transient LLM failures.
- Splits reflector activity routing into engineering, life/operations, and creative/professional sections for cleaner startup context.

## Validation

Local validation:

```bash
make check
make build
uv run twine check dist/*
claude plugin validate src/observational_memory/cowork_plugin
```

Release candidate smoke tests:

```bash
uv tool run --isolated --from ./dist/observational_memory-0.5.4-py3-none-any.whl om --version
uv tool run --isolated --from ./dist/observational_memory-0.5.4-py3-none-any.whl om observe --transcript tests/fixtures/cowork-audit.jsonl --source cowork --dry-run
```

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.4
git push origin main
git push origin v0.5.4
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.4`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.4
```

Suggested release notes:

````markdown
## Highlights

- Adds first-class Claude Cowork support: local plugin install, SessionStart memory injection, checkpoint hooks, `/recall`, and Cowork `audit.jsonl` ingestion.
- Validates Cowork plugin hook shape in `om doctor`, catching stale plugin installs that do not match the current Claude plugin schema.
- Improves LLM call resilience with transient retry handling and operation-specific provider inference.
- Expands startup context routing so active memory can include engineering, life/operations, and creative/professional work.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.5.4
om install --cowork
```
````
