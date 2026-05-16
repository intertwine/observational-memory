#!/usr/bin/env bash
# Observational Memory — Grok Build TUI SessionStart Hook
#
# This hook is installed via `om install --grok`.
#
# Grok supports the same hook schema as Claude Code and also reads
# ~/.claude/settings.json for compatibility. The installer avoids
# registering a duplicate SessionStart when OM Claude hooks are already
# present, to prevent double injection of profile + active context.
#
# This script falls back to a direct `om context` call (search-backed) and
# outputs the additionalContext JSON that Grok understands.
set -euo pipefail

MEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/observational-memory"
PROFILE="$MEM_DIR/profile.md"
ACTIVE="$MEM_DIR/active.md"

# Find the om command (prefer the one in PATH, fall back to common locations)
OM="$(command -v om 2>/dev/null || true)"
if [[ -z "$OM" && -x "$HOME/.local/bin/om" ]]; then
    OM="$HOME/.local/bin/om"
fi

# Preferred path: use `om context` (search-backed, respects Cluster, budget-aware)
if [[ -n "$OM" ]]; then
    if "$OM" context 2>/dev/null; then
        exit 0
    fi
fi

# Fallback: compact file dump (profile + active)
context=""

if [[ -f "$PROFILE" && -s "$PROFILE" ]]; then
    context+="$(cat "$PROFILE")

---
"
fi

if [[ -f "$ACTIVE" && -s "$ACTIVE" ]]; then
    context+="$(cat "$ACTIVE")"
fi

if [[ -n "$context" ]]; then
    if ! command -v jq >/dev/null 2>&1; then
        echo "observational-memory Grok hook fallback requires jq to emit context JSON" >&2
        exit 0
    fi
    # Grok (via Claude compatibility or native hooks) understands this shape
    jq -n --arg ctx "$context" '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ctx
        }
    }'
fi
