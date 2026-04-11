# Release 0.5.1

This branch is prepared for publishing `observational-memory` `0.5.1`.

## What's in 0.5.1

- Fixed OpenAI GPT-5/o-series Chat Completions compatibility by sending `max_completion_tokens` for affected model families while preserving `max_tokens` for older chat models.
- Added regression coverage for `gpt-5.4`, `gpt-5.2-chat-latest`, `o4-mini`, and `gpt-4o-mini` token-limit request parameters.
- Added a maintainer "machine green" checklist for post-merge, reinstall, and release-fix validation across OM status, live LLM validation, QMD embeddings, and scheduler health.

## Publish From This Branch

Run these commands from `main` after PRs #29 and #30 are merged.

```bash
git checkout main
git pull --ff-only
make check
make build
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.5.1.tar.gz`
- `dist/observational_memory-0.5.1-py3-none-any.whl`

## Candidate Validation

Smoke-test the built wheel directly:

```bash
om_rc() {
  uv tool run --isolated --from ./dist/observational_memory-0.5.1-py3-none-any.whl om "$@"
}

om_rc --version
om_rc doctor --validate-key
```

Then confirm:

- `om_rc --version` prints `0.5.1`
- `om_rc doctor --validate-key` succeeds with a GPT-5-class OpenAI model such as `gpt-5.4`
- QMD status and embeddings remain healthy after the upgrade

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit on `main` and push the tag:

```bash
git tag v0.5.1
git push origin main
git push origin v0.5.1
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.1`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.1
```

Suggested release notes:

````markdown
## Highlights

- Fixed OpenAI GPT-5/o-series compatibility by using `max_completion_tokens` where required while keeping older chat model behavior unchanged.
- Added regression tests for GPT-5, GPT-5 chat alias, o-series, and `gpt-4o-mini` request parameter handling.
- Documented the maintainer "machine green" checklist for validating OM, QMD, live provider access, and scheduler health after upgrades.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool upgrade --reinstall observational-memory==0.5.1
```
````

## Post-Release Checks

Verify PyPI:

```bash
uv run python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.5.1/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.5.1
```
