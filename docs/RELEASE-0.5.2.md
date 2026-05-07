# Release 0.5.2

This branch is prepared for publishing `observational-memory` `0.5.2`.

## What's in 0.5.2

- Updated Codex hook feature detection for the 0.129.0 rename from `[features].codex_hooks` to `[features].hooks`.
- Kept backward compatibility with older Codex CLI releases by writing both `hooks = true` and `codex_hooks = true` during `om install --codex`.
- Added regression coverage for canonical and legacy Codex hook flags in installer and doctor flows.
- Documented the Codex flag transition in user and maintainer docs.

## Compatibility Notes

Validation covered both sides of the Codex transition:

- `@openai/codex@0.114.0` requires `codex_hooks = true`.
- `@openai/codex@0.129.0` exposes `hooks` as the stable feature while accepting `codex_hooks` as a legacy alias.

For development commands that need current npm packages on a machine with `min-release-age` configured, use a command-scoped override instead of changing global npm policy:

```bash
npm --min-release-age=0 exec -y --package=@openai/codex@0.129.0 -- codex features list
```

## Publish From This Branch

Run these commands from this release branch or from `main` after merging.

```bash
make check
make build
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.5.2.tar.gz`
- `dist/observational_memory-0.5.2-py3-none-any.whl`

## Candidate Validation

Smoke-test the built wheel directly:

```bash
om_rc() {
  uv tool run --isolated --from ./dist/observational_memory-0.5.2-py3-none-any.whl om "$@"
}

om_rc --version
om_rc doctor --validate-key
```

Then confirm:

- `om_rc --version` prints `0.5.2`
- `om_rc doctor --validate-key` succeeds with the configured provider
- `om install --codex` writes both Codex hook feature flags in a temporary `CODEX_HOME`

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.2
git push origin codex/hooks-flag-compat
git push origin v0.5.2
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.2`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.2
```

Suggested release notes:

````markdown
## Highlights

- Updated Codex hook feature handling for the `codex_hooks` -> `hooks` flag rename in Codex CLI 0.129.0.
- `om install --codex` now writes both hook feature flags so current Codex and older 0.114-era Codex releases both work.
- `om doctor` and `om status` now accept either flag as enabled.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool upgrade --reinstall observational-memory==0.5.2
```
````

## Post-Release Checks

Verify PyPI:

```bash
uv run python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.5.2/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.5.2
```
