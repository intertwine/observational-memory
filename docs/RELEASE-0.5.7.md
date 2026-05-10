# Release 0.5.7

This branch is prepared as the `observational-memory` `0.5.7` release candidate.

## What's in 0.5.7

- Adds Windows 10/11 compatibility for the CLI, installer, scheduler, and default memory/config paths.
- Uses Windows Task Scheduler (`schtasks.exe`) for background observation on Windows.
- Emits Windows-friendly Claude hook commands that call `om` directly instead of depending on `bash` or `jq`.
- Keeps Cowork macOS-only and documents that behavior clearly on Windows.
- Adds a Windows compatibility test suite covering paths, install behavior, hook generation, and scheduler wiring.

## Validation

Local validation:

```bash
make check
make build
uv run twine check dist/*
```

Release candidate smoke tests:

```bash
uv tool run --isolated --from ./dist/observational_memory-0.5.7-py3-none-any.whl om --version
uv tool run --isolated --from ./dist/observational_memory-0.5.7-py3-none-any.whl om doctor
uv tool run --isolated --from ./dist/observational_memory-0.5.7-py3-none-any.whl om install --help
```

## Publish To PyPI

After validation succeeds and the release commit is ready:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.7
git push origin main
git push origin v0.5.7
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.7`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.7
```

Suggested release notes:

````markdown
## Highlights

- Adds Windows 10/11 compatibility for `om`, including Windows-native memory/config directories, Claude hook commands, and Task Scheduler support.
- Keeps the macOS/Linux behavior intact while making the installer choose the right scheduler per platform.
- Documents the Windows substitutions for paths, hooks, scheduling, and Cowork support.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.5.7
om install
```

## Windows install

```powershell
uv tool install observational-memory==0.5.7
om install
om doctor
```
````
