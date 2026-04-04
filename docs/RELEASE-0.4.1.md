# Release 0.4.1

This branch is prepared for publishing `observational-memory` `0.4.1`.

## What's in 0.4.1

- Added a root `om --version` flag for quick package verification after install and upgrade.
- Surfaced Hermes as a first-class manual observe source in the CLI:
  - `om observe --source hermes`
  - `om observe --transcript ~/.hermes/sessions/... --source hermes`
- Expanded the README and maintainer docs to explain Hermes session-log ingestion truthfully, including current scope and limits.
- Added CLI and parser coverage for the new Hermes-facing surface and version flag.

## Publish From This Branch

Run these commands from a checkout of branch `codex/om-0.4.1-release-pr22`.

```bash
git checkout codex/om-0.4.1-release-pr22
git pull --ff-only
make check
make build
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.4.1.tar.gz`
- `dist/observational_memory-0.4.1-py3-none-any.whl`

## Candidate Validation

Upgrade the local uv tool from the branch build or from TestPyPI if you publish there first:

```bash
uv tool upgrade --reinstall observational-memory==0.4.1
```

Recommended validation flow:

```bash
om --version
om doctor
om observe --source hermes --dry-run
```

Then confirm:

- `om --version` prints `0.4.1`
- `om doctor` is healthy for the current local setup
- README examples for Hermes and `--version` match real CLI behavior

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same commit and push the tag:

```bash
git tag v0.4.1
git push origin codex/om-0.4.1-release-pr22
git push origin v0.4.1
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.4.1`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.4.1
```

Suggested release notes:

````markdown
## Highlights

- Added a root `om --version` flag for faster install and upgrade verification.
- Surfaced Hermes as a supported manual observe source in the CLI and documented how it fits into shared memory.
- Tightened README and maintainer docs so the Hermes scope is explicit and examples match the shipped behavior.

## Upgrade

Homebrew:

`brew update`

`brew upgrade observational-memory`

PyPI / uv tool:

`uv tool upgrade --reinstall observational-memory==0.4.1`
````

## Post-Release Checks

Verify PyPI:

```bash
uv run python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.4.1/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.4.1
```
