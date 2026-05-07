# Release 0.5.5

This branch is prepared for publishing `observational-memory` `0.5.5`.

## What's in 0.5.5

- Supersedes `0.5.4` with a scheduler target-scope fix for Cowork-only installs.
- Keeps `om install --cowork` focused on the Cowork plugin and prevents `--cowork --no-cron` from removing existing Codex, auto-memory, or reflector backstops.
- Preserves the `0.5.4` Cowork integration: transcript ingestion, plugin install, validated hook schema, `/recall`, and the bundled skill.

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
uv tool run --isolated --from ./dist/observational_memory-0.5.5-py3-none-any.whl om --version
uv tool run --isolated --from ./dist/observational_memory-0.5.5-py3-none-any.whl om observe --transcript tests/fixtures/cowork-audit.jsonl --source cowork --dry-run
```

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.5
git push origin main
git push origin v0.5.5
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.5`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.5
```

Suggested release notes:

````markdown
## Highlights

- Fixes Cowork-only scheduler scoping so `om install --cowork --no-cron` no longer removes existing launchd/cron backstops for Codex, auto-memory, or reflection.
- Carries forward the Cowork support from `0.5.4`: local plugin install, SessionStart memory injection, checkpoint hooks, `/recall`, and Cowork `audit.jsonl` ingestion.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.5.5
om install --cowork
```
````
