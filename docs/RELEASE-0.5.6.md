# Release 0.5.6

This branch is prepared as the `observational-memory` `0.5.6` release candidate.

## What's in 0.5.6

- Adds `om export` for reviewed platform-native memory seed bundles.
- Supports `--target chatgpt` for a concise ChatGPT Memory/project seed.
- Supports `--target claude-managed-agents` for small, focused markdown files that can seed Claude Managed Agents memory stores.
- Documents the compatibility plan for ChatGPT Memory, ChatGPT Pulse, Claude Managed Agents memory stores, and Anthropic dreaming.

## Validation

Local validation:

```bash
make check
make build
uv run twine check dist/*
```

Release candidate smoke tests:

```bash
uv tool run --isolated --from ./dist/observational_memory-0.5.6-py3-none-any.whl om --version
uv tool run --isolated --from ./dist/observational_memory-0.5.6-py3-none-any.whl om export --target chatgpt --output /tmp/om-chatgpt-export
uv tool run --isolated --from ./dist/observational_memory-0.5.6-py3-none-any.whl om export --target claude-managed-agents --output /tmp/om-claude-export
```

## Publish To PyPI

After validation succeeds and the PR is merged:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.5.6
git push origin main
git push origin v0.5.6
```

The tag push triggers `.github/workflows/homebrew-release.yml`, which regenerates the Homebrew formula from PyPI and updates the tap repo.

Do not push the tag before PyPI has `0.5.6`, or the Homebrew workflow will fail.

## GitHub Release Notes

Suggested release title:

```text
v0.5.6
```

Suggested release notes:

````markdown
## Highlights

- Adds `om export` for reviewed memory seed bundles targeting ChatGPT Memory, Claude Managed Agents memory stores, and generic file-consuming memory systems.
- Keeps OM local-first: exports are reviewable files, not automatic writes into hosted memory.
- Documents how ChatGPT Memory/Pulse and Claude Managed Agents memory/dreaming map onto OM's local `profile.md`, `active.md`, `reflections.md`, and `observations.md`.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.5.6
om install
```

## Platform memory export

```bash
om export --target chatgpt
om export --target claude-managed-agents
```
````
