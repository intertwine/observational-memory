# Release 0.5.0

This branch is prepared for publishing `observational-memory` `0.5.0`.

## What's in 0.5.0

- Upgraded OM's QMD integration for QMD `2.1.x`, including dedicated index isolation and QMD-specific config knobs.
- Added truthful QMD health and embedding visibility to `om status` and `om doctor`.
- Added repo-local QMD benchmark fixtures and maintainer workflow for `qmd bench`.
- Surfaced source-path and source-line metadata in `om search` results and JSON output when available.
- Added `om search --raw-qmd` for native QMD CLI passthrough output.
- Hardened local search behavior after machine-level e2e validation:
  - BM25 now falls back for zero-IDF common-term edge cases.
  - OM recovers legacy QMD manifest metadata even when QMD lowercases old filenames.
  - `OM_QMD_NO_RERANK=1` now stays on QMD's fast typed `lex` + `vec` path instead of triggering expansion-model downloads.

## Publish From This Branch

Run these commands from a checkout of branch `codex/om-0.5.0-release`.

```bash
git checkout codex/om-0.5.0-release
git pull --ff-only
make check
make build
```

Expected artifacts after `make build`:

- `dist/observational_memory-0.5.0.tar.gz`
- `dist/observational_memory-0.5.0-py3-none-any.whl`

## Candidate Validation

Smoke-test the built wheel directly:

```bash
om_rc() {
  uv tool run --isolated --from ./dist/observational_memory-0.5.0-py3-none-any.whl om "$@"
}

om_rc --version
```

If you are validating the QMD path, use QMD `>= 2.1.0`.

Recommended validation flow:

```bash
om_rc --version
om_rc status
om_rc doctor
OM_SEARCH_BACKEND=qmd om_rc search --reindex "launchd"
OM_SEARCH_BACKEND=qmd-hybrid OM_QMD_NO_RERANK=1 om_rc search "launchd"
OM_SEARCH_BACKEND=qmd-hybrid om_rc search "launchd" --json
OM_SEARCH_BACKEND=qmd-hybrid om_rc search "launchd" --raw-qmd
```

If QMD `2.1.x` is available locally, also run:

```bash
make qmd-bench-preflight
make qmd-bench
```

Then confirm:

- `om_rc --version` prints `0.5.0`
- `om_rc status` and `om_rc doctor` agree about QMD install health, collection readiness, and embedding state
- `OM_QMD_NO_RERANK=1` is reported truthfully and returns fast hybrid results
- `om_rc search --json` exposes `source_path`, `source_line`, `qmd_file`, `qmd_docid`, and `qmd_line` when available
- `om_rc search --raw-qmd` preserves native QMD output without OM banners mixed into stdout
- the repo-local benchmark fixture still runs against a QMD `2.1.x` install

## Publish To PyPI

After validation succeeds:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same commit and push the tag:

```bash
git tag v0.5.0
git push origin codex/om-0.5.0-release
git push origin v0.5.0
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.0`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.0
```

Suggested release notes:

```markdown
## Highlights

- Upgraded OM's QMD integration for QMD 2.1 with dedicated index isolation, QMD-specific config knobs, and richer search observability.
- Added source-path and source-line metadata to `om search` output and `--json`, plus native `--raw-qmd` passthrough for advanced workflows.
- Added repo-local QMD benchmark fixtures and maintainer tooling for `qmd bench`.
- Hardened search behavior with machine-level e2e fixes for BM25 zero-IDF edge cases, legacy QMD filename normalization, and fast no-rerank hybrid queries.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool upgrade --reinstall observational-memory==0.5.0
```
```

## Post-Release Checks

Verify PyPI:

```bash
uv run python - <<'PY'
from urllib.request import urlopen
print(urlopen("https://pypi.org/pypi/observational-memory/0.5.0/json").status)
PY
```

Verify Homebrew workflow:

```bash
gh run list --workflow homebrew-release.yml --limit 5
```

Verify latest GitHub release:

```bash
gh release view v0.5.0
```
