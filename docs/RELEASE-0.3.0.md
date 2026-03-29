# Release 0.3.0

This branch is prepared for publishing `observational-memory` `0.3.0`.

## What's in 0.3.0

- Codex startup is now hooks-first via global `SessionStart` integration in `~/.codex/hooks.json`.
- Codex checkpointing is now hooks-first via a global `Stop` hook that queues transcript-specific observation.
- `~/.codex/AGENTS.md` is now a conditional fallback instead of the primary Codex startup path.
- Codex cron polling remains installed as a backstop when hooks are unavailable or a session exits before `Stop`.
- Status, doctor, and install flows now surface the Codex hooks feature and hook health explicitly.
- README and maintainer docs now describe the shipped hooks-first Codex model.

## Publish From This Branch

Run these commands from a checkout of branch `codex/release-0.3.0-prep`.

```bash
git checkout codex/release-0.3.0-prep
git pull --ff-only
make check
make build
make publish-test
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.3.0.tar.gz`
- `dist/observational_memory-0.3.0-py3-none-any.whl`

## Candidate Validation

Upgrade the local uv tool from TestPyPI:

```bash
uv tool upgrade --reinstall \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  observational-memory==0.3.0
```

Recommended end-to-end checks:

```bash
om status
om doctor
om install --codex --provider openai --llm-model gpt-4o-mini --non-interactive
```

Then verify:

- `~/.codex/config.toml` contains `codex_hooks = true`
- `~/.codex/hooks.json` contains OM-managed `SessionStart` and `Stop`
- `~/.codex/AGENTS.md` contains conditional fallback wording

## Publish To PyPI

After the TestPyPI validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same commit and push the tag:

```bash
git tag v0.3.0
git push origin codex/release-0.3.0-prep
git push origin v0.3.0
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.3.0`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.3.0
```

Suggested release notes:

```markdown
## Highlights

- Added hooks-first Codex startup integration via a global `SessionStart` hook.
- Added hooks-first Codex checkpointing via a global `Stop` hook and transcript-specific checkpoint runner.
- Kept Codex graceful fallback behavior with conditional `AGENTS.md` instructions and cron backstop polling.
- Expanded install, status, and doctor coverage for Codex hooks health.
- Updated user and maintainer docs to reflect the new hooks-first Codex model.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool upgrade --reinstall observational-memory==0.3.0
```
```

## Post-Release Checks

Verify PyPI:

```bash
python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.3.0/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.3.0
```
