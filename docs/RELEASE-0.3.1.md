# Release 0.3.1

This branch is prepared for publishing `observational-memory` `0.3.1`.

## What's in 0.3.1

- Added native macOS `launchd` scheduler support and made it the default background backstop on macOS.
- Kept cron as the default backstop on non-macOS Unix-like platforms and as an explicit opt-in on macOS.
- Made scheduler setup failures non-fatal so hooks-first installs still succeed when background job setup has problems.
- Added scheduler-aware `om status` / `om doctor` reporting for launchd vs cron, including duplicate-backstop detection on macOS.
- Hardened cron read/write paths with explicit timeouts so `crontab` stalls downgrade to warnings instead of hanging install flows.

## Publish From This Branch

Run these commands from a checkout of branch `codex/om-launchd-pr3`.

```bash
git checkout codex/om-launchd-pr3
git pull --ff-only
make check
make build
make publish-test
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.3.1.tar.gz`
- `dist/observational_memory-0.3.1-py3-none-any.whl`

## Candidate Validation

Upgrade the local uv tool from TestPyPI:

```bash
uv tool upgrade --reinstall \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  observational-memory==0.3.1
```

Recommended validation flow:

```bash
om install --codex --provider openai --llm-model gpt-4o-mini --non-interactive
om status
om doctor
```

On macOS, also verify the LaunchAgents directly:

```bash
launchctl print gui/$(id -u)/com.intertwine.observational-memory.codex-observe
launchctl print gui/$(id -u)/com.intertwine.observational-memory.auto-memory
launchctl print gui/$(id -u)/com.intertwine.observational-memory.reflect
```

Then confirm:

- Codex hooks are still present in `~/.codex/hooks.json`
- `~/.codex/AGENTS.md` still contains only the conditional OM fallback block
- `om doctor` reports the scheduler truthfully for the current platform
- macOS default installs do not leave OM cron jobs behind alongside LaunchAgents unless cron was explicitly selected

## Publish To PyPI

After the TestPyPI validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same commit and push the tag:

```bash
git tag v0.3.1
git push origin codex/om-launchd-pr3
git push origin v0.3.1
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.3.1`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.3.1
```

Suggested release notes:

```markdown
## Highlights

- Added native macOS `launchd` scheduler support for OM background backstops.
- Kept hooks-first Codex behavior while making scheduler failures non-fatal during install.
- Added scheduler-aware `om status` and `om doctor` reporting, including duplicate-backstop detection on macOS.
- Hardened cron subprocess handling with explicit timeouts so scheduler problems warn instead of hanging.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool upgrade --reinstall observational-memory==0.3.1
```
```

## Post-Release Checks

Verify PyPI:

```bash
uv run python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.3.1/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.3.1
```
