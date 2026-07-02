#!/usr/bin/env bash
# Observational Memory — Claude Code SessionEnd / checkpoint hook.
# Compatibility shim: delegate checkpoint handling to the bounded Python worker
# path so shell hooks never spawn long-running direct `om observe` processes.
set -euo pipefail

ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/observational-memory/env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

INPUT="$(cat)"

OM="$(command -v om 2>/dev/null || true)"
if [[ -z "$OM" ]]; then
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
    exit 0
fi

printf '%s' "$INPUT" | "$OM" claude-checkpoint
