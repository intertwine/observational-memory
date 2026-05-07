# Release 0.5.3

This branch is prepared for publishing `observational-memory` `0.5.3`.

## What's in 0.5.3

- Includes the Codex hook feature flag compatibility from `0.5.2`: writes both `[features].hooks = true` and `[features].codex_hooks = true`, and treats either as enabled.
- Hardens cron detection so `om status` and `om doctor` warn instead of crashing when macOS/Homebrew sandboxing denies `crontab -l`.
- Adds regression coverage for the sandboxed `crontab` permission-denied path.

## Validation

Local validation:

```bash
make check
make build
uv run twine check dist/*
```

Release candidate smoke tests:

```bash
uv tool run --isolated --from ./dist/observational_memory-0.5.3-py3-none-any.whl om --version
brew test intertwine/tap/observational-memory
```

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.3
git push origin main
git push origin v0.5.3
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.3`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.3
```

Suggested release notes:

````markdown
## Highlights

- Carries forward Codex CLI hook flag compatibility for the `codex_hooks` -> `hooks` rename.
- Fixes `om status` in Homebrew/macOS sandbox contexts where `crontab -l` raises `PermissionError`; the CLI now reports a warning instead of crashing.
- Adds regression coverage for the crontab permission-denied path.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.5.3
```
````
