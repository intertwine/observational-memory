#!/usr/bin/env bash
# Observational Memory — Claude Code SessionStart Hook
# Tries search-backed context first, falls back to full file dump.
set -euo pipefail

MEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/observational-memory"
PROFILE="$MEM_DIR/profile.md"
ACTIVE="$MEM_DIR/active.md"
REF="$MEM_DIR/reflections.md"
OBS="$MEM_DIR/observations.md"

# Find the om command
OM="$(command -v om 2>/dev/null || true)"

# Layer 1: om context (search-backed retrieval)
if [[ -n "$OM" ]]; then
    "$OM" context 2>/dev/null && exit 0
fi

# Layer 2: Compact file dump (fallback if om not found or context failed)
context=""

if [[ -f "$PROFILE" ]] && [[ -s "$PROFILE" ]]; then
    profile_content=$(cat "$PROFILE")
    context+="$profile_content

---

"
fi

if [[ -f "$ACTIVE" ]] && [[ -s "$ACTIVE" ]]; then
    active_content=$(cat "$ACTIVE")
    context+="$active_content"
fi

# Backward-compatible fallback for older installs without compact files yet.
if [[ -z "$context" ]]; then
    if [[ -f "$REF" ]] && [[ -s "$REF" ]]; then
        ref_content=$(cat "$REF")
        context+="## Long-Term Memory (Reflections)

$ref_content

---

"
    fi

    if [[ -f "$OBS" ]] && [[ -s "$OBS" ]]; then
        obs_content=$(cat "$OBS")
        context+="## Recent Observations

$obs_content"
    fi
fi

if [[ -n "$context" ]]; then
    jq -n --arg ctx "$context" '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ctx
        }
    }'
fi
