#!/usr/bin/env bash
# Observational Memory — Claude Code SessionStart Hook
# Reads memory files and injects them as additionalContext.
set -euo pipefail

MEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/observational-memory"
REF="$MEM_DIR/reflections.md"
OBS="$MEM_DIR/observations.md"

context=""

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

if [[ -n "$context" ]]; then
    jq -n --arg ctx "$context" '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $ctx
        }
    }'
fi
