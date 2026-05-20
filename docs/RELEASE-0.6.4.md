# Release 0.6.4

Released shortly after 0.6.3.

## Summary

`v0.6.4` is a small but important stability release. It raises the `SessionStart` hook timeout from 5 s to 15 s for Claude Code, Codex, and Grok (and the Cowork plugin). This prevents occasional "startup is timing out" failures when `om context` is invoked on cold Python starts, very large memory stores, or when a bounded OM Cluster pull is configured.

## User-Facing Changes

- `SessionStart` hooks (the ones that deliver `om context` at agent startup) are now registered with a 15-second timeout instead of 5 seconds.
- The change affects:
  - Claude Code (`~/.claude/settings.json`)
  - Codex (`~/.codex/hooks.json`)
  - Grok (`~/.grok/hooks/observational-memory.json` or via Claude compatibility)
  - Cowork local plugin
- No behavior change for users who were already succeeding; users who occasionally saw the hook killed by the host now succeed reliably.

## New Maintainer Tool

A permanent regression test was added:

```bash
make verify-session-start
# or
uv run python scripts/verify_session_start_hooks.py --keep
```

See `docs/MAINTAINERS.md` and the script header for details. It fully simulates what Claude, Codex, and Grok do on session start and is the authoritative way to prove the fix stays fixed.

## Validation

```bash
git status --short --branch
make check
make verify-session-start
OM_CLUSTER_ENABLED=0 uv run om context >/tmp/om-context.json
uv run om recall --query "current work" --limit 3
```

The 5 s → 15 s bump is the only user-visible change. No other behavior, file formats, or APIs were modified.

## Compatibility

Fully compatible with 0.6.3. Existing installations will pick up the new timeout the next time `om install --claude --codex --grok` (or `--all`) is run.