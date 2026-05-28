#!/usr/bin/env bash
# Observational Memory — Claude Code SessionStart Hook
# `om context` is the only startup-context producer. It is search-backed and
# enforces the startup budget, dedup, freshness, cwd/task routing, and recall
# handles. We never cat raw generated memory files as a fallback (see #67).
set -euo pipefail

# Find the om command
OM="$(command -v om 2>/dev/null || true)"

# `om context` is the sole context producer. On any failure (om missing or
# non-zero exit) we fail closed: emit no agent context, write one diagnostic
# line to stderr, and exit cleanly.
if [[ -n "$OM" ]] && "$OM" context --for claude --cwd "$PWD" 2>/dev/null; then
    exit 0
fi

echo "observational-memory: om context unavailable - run om doctor" >&2
exit 0
