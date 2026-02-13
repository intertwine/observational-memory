#!/usr/bin/env bash
# Observational Memory — Claude Code SessionEnd / checkpoint hook.
# Triggers the observer on session completion or periodic checkpoints for long sessions.
set -euo pipefail

# Source API keys from env file
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/observational-memory/env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

MEM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/observational-memory"
STATE_FILE="$MEM_DIR/.session-observer-state.json"
INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
EVENT_NAME=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
DISABLE_CHECKPOINTS="${OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS:-0}"

if [[ -z "$TRANSCRIPT" ]] || [[ ! -f "$TRANSCRIPT" ]]; then
    exit 0
fi

# Interval between in-session checkpoints (set 0 to disable throttling).
THROTTLE_SECONDS="${OM_SESSION_OBSERVER_INTERVAL_SECONDS:-900}"
if ! [[ "$THROTTLE_SECONDS" =~ ^[0-9]+$ ]]; then
    THROTTLE_SECONDS=900
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

is_force_event=false
is_checkpoint_event=false
case "$EVENT_NAME" in
    UserPromptSubmit|PreCompact)
        is_checkpoint_event=true
        ;;
    SessionEnd|Stop)
        is_force_event=true
        ;;
    "")
        is_force_event=true
        ;;
esac

mkdir -p "$MEM_DIR"

if [[ "$is_checkpoint_event" == true ]] && [[ "$THROTTLE_SECONDS" -gt 0 ]]; then
    case "$(printf '%s' "$DISABLE_CHECKPOINTS" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            exit 0
            ;;
    esac

    # Keep only the latest line_count even when skipping throttled events.
    if [[ "$THROTTLE_SECONDS" -eq 0 ]]; then
        :
    else
        now=$(date +%s)
        if [[ -f "$STATE_FILE" ]]; then
            last_seen_lines=$(jq -r --arg p "$TRANSCRIPT" '.[$p].line_count // empty' "$STATE_FILE")
            last_observed_at=$(jq -r --arg p "$TRANSCRIPT" '.[$p].last_observed // empty' "$STATE_FILE")
        else
            last_seen_lines=""
            last_observed_at=""
        fi

        transcript_lines="$(wc -l < "$TRANSCRIPT" | tr -d ' ')"

        if [[ -n "$last_seen_lines" ]] && (( transcript_lines <= last_seen_lines )); then
            # No new transcript lines to process.
            exit 0
        fi

        if [[ -n "$last_observed_at" ]] && (( now - last_observed_at < THROTTLE_SECONDS )); then
            # Keep checkpoint state updated so we continue from latest lines if we skipped.
            state_tmp="$(mktemp)"
            jq -n --arg p "$TRANSCRIPT" --argjson lc "$transcript_lines" \
                --argjson lo "$last_observed_at" \
                '.[$p] = {last_observed: $lo, line_count: $lc}' < /dev/null > "$state_tmp"
            if [[ -s "$STATE_FILE" ]] && jq empty "$STATE_FILE" >/dev/null 2>&1; then
                jq --arg p "$TRANSCRIPT" --argjson lc "$transcript_lines" \
                    --argjson lo "$last_observed_at" \
                    '.[$p] = {last_observed: $lo, line_count: $lc}' \
                    "$STATE_FILE" > "$state_tmp"
            fi
            mv "$state_tmp" "$STATE_FILE"
            exit 0
        fi
    fi
elif [[ "$is_force_event" == false ]]; then
    # Unknown event type; default to in-session throttled behavior so we do not over-observe.
    now=$(date +%s)
    if [[ -f "$STATE_FILE" ]]; then
        last_seen_lines=$(jq -r --arg p "$TRANSCRIPT" '.[$p].line_count // empty' "$STATE_FILE")
        last_observed_at=$(jq -r --arg p "$TRANSCRIPT" '.[$p].last_observed // empty' "$STATE_FILE")
    else
        last_seen_lines=""
        last_observed_at=""
    fi

    transcript_lines="$(wc -l < "$TRANSCRIPT" | tr -d ' ')"

    if [[ -n "$last_seen_lines" ]] && (( transcript_lines <= last_seen_lines )); then
        # No new transcript lines to process.
        exit 0
    fi

    if [[ -n "$last_observed_at" ]] && (( now - last_observed_at < THROTTLE_SECONDS )); then
        # Keep checkpoint state updated so we continue from latest lines if we skipped.
        state_tmp="$(mktemp)"
        jq -n --arg p "$TRANSCRIPT" --argjson lc "$transcript_lines" \
            --argjson lo "$last_observed_at" \
            '.[$p] = {last_observed: $lo, line_count: $lc}' < /dev/null > "$state_tmp"
        if [[ -s "$STATE_FILE" ]] && jq empty "$STATE_FILE" >/dev/null 2>&1; then
            jq --arg p "$TRANSCRIPT" --argjson lc "$transcript_lines" \
                --argjson lo "$last_observed_at" \
                '.[$p] = {last_observed: $lo, line_count: $lc}' \
                "$STATE_FILE" > "$state_tmp"
        fi
        mv "$state_tmp" "$STATE_FILE"
        exit 0
    fi
fi

# Run observer in background so we don't block session lifecycle.
(
    "$OM" observe --transcript "$TRANSCRIPT" --source claude
    observe_status=$?
    if [[ $observe_status -eq 0 ]]; then
        mkdir -p "$MEM_DIR"
        now=$(date +%s)
        transcript_lines="$(wc -l < "$TRANSCRIPT" | tr -d ' ')"
        state_tmp="$(mktemp)"
        jq -n --arg p "$TRANSCRIPT" --argjson now_ts "$now" --argjson lc "$transcript_lines" \
            '.[$p] = {last_observed: $now_ts, line_count: $lc}' < /dev/null > "$state_tmp"
        if [[ -s "$STATE_FILE" ]] && jq empty "$STATE_FILE" >/dev/null 2>&1; then
            jq --arg p "$TRANSCRIPT" --argjson now_ts "$now" --argjson lc "$transcript_lines" \
                '.[$p] = {last_observed: $now_ts, line_count: $lc}' \
                "$STATE_FILE" > "$state_tmp"
        fi
        mv "$state_tmp" "$STATE_FILE"
    fi
) &
disown

exit 0
