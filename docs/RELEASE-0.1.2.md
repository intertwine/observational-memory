# Release 0.1.2

This branch is prepared for publishing `observational-memory` `0.1.2`.

## What's in 0.1.2

- Compact startup memory views (`profile.md` + `active.md`) to reduce default session-start context size while keeping deeper memory available on demand.
- Reflection catch-up after missed daily runs.
- Safer cron block cleanup during reinstall/uninstall.
- Fixed Codex Desktop transcript parsing for modern `response_item.payload` message records.
- Fixed the `.jsonl` edge case where a single full-JSON payload like `{"items":[...]}` could parse as zero messages.

## Publish From The Other Machine

Run these commands from a checkout of branch `claude/release-0.1.2-prep`.

```bash
git checkout claude/release-0.1.2-prep
git pull --ff-only
make test
make build
make publish
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.1.2.tar.gz`
- `dist/observational_memory-0.1.2-py3-none-any.whl`

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same commit and push the tag:

```bash
git tag v0.1.2
git push origin claude/release-0.1.2-prep
git push origin v0.1.2
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.1.2`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.1.2
```

Suggested release notes:

```markdown
## Highlights

- Added compact startup memory views (`profile.md` + `active.md`) so default session priming is smaller and more focused, while `reflections.md` / `observations.md` remain available for deeper retrieval.
- Added reflection catch-up after missed daily runs, which helps laptops recover when the scheduled reflector cron is skipped during sleep.
- Fixed Codex Desktop transcript parsing so modern `response_item.payload` chat messages are ingested correctly.
- Fixed a `.jsonl` parser edge case where a single full-JSON payload could parse as zero messages.
- Hardened cron cleanup so malformed observational-memory blocks do not silently drop unrelated cron entries.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --upgrade observational-memory
```
```

## Post-Release Checks

Verify PyPI:

```bash
python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.1.2/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.1.2
```
