#!/usr/bin/env bash
# Observational Memory — Grok Build TUI SessionStart Hook
#
# This hook is installed via `om install --grok`.
#
# Grok supports the same hook schema as Claude Code and also reads
# ~/.claude/settings.json for compatibility. The installer avoids
# registering a duplicate SessionStart when OM Claude hooks are already
# present, to prevent double injection of startup context.
#
# `om context` is the only startup-context producer. It is search-backed and
# enforces the startup budget, dedup, freshness, cwd/task routing, and recall
# handles. We never cat raw generated memory files as a fallback (see #67).
set -euo pipefail

# Find the om command (prefer the one in PATH, fall back to common locations)
OM="$(command -v om 2>/dev/null || true)"
if [[ -z "$OM" && -x "$HOME/.local/bin/om" ]]; then
    OM="$HOME/.local/bin/om"
fi

# `om context` is the sole context producer. On any failure (om missing or
# non-zero exit) we fail closed: emit no agent context, write one diagnostic
# line to stderr, and exit cleanly.
if [[ -n "$OM" ]] && "$OM" context --for grok --cwd "$PWD" 2>/dev/null; then
    exit 0
fi

echo "observational-memory: om context unavailable - run om doctor" >&2
exit 0
