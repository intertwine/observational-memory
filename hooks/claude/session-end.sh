#!/usr/bin/env bash
# Observational Memory — Claude Code SessionEnd Hook
# Triggers the observer on the just-completed conversation.
set -euo pipefail

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')

if [[ -z "$TRANSCRIPT" ]] || [[ ! -f "$TRANSCRIPT" ]]; then
    exit 0
fi

# Find om command
OM=$(command -v om 2>/dev/null || echo "")
if [[ -z "$OM" ]]; then
    # Try common uv tool locations
    for candidate in \
        "$HOME/.local/bin/om" \
        "$HOME/.cargo/bin/om" \
        "$HOME/.local/share/uv/tools/observational-memory/bin/om"; do
        if [[ -x "$candidate" ]]; then
            OM="$candidate"
            break
        fi
    done
fi

if [[ -z "$OM" ]]; then
    exit 0  # om not installed, skip silently
fi

# Run observer in background so we don't block session exit
"$OM" observe --transcript "$TRANSCRIPT" --source claude &
disown

exit 0
