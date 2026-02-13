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
LOCK_DIR="$MEM_DIR/.session-observer-locks"
LOCK_STALE_MINUTES="${OM_SESSION_OBSERVER_LOCK_STALE_MINUTES:-60}"
INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty')
EVENT_NAME=$(echo "$INPUT" | jq -r '.hook_event_name // empty')
DISABLE_CHECKPOINTS="${OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS:-0}"

# STATE_FILE is hook-local checkpoint throttling metadata. It is separate from
# the Python observer cursor file used for incremental transcript processing.

if [[ -z "$TRANSCRIPT" ]] || [[ ! -f "$TRANSCRIPT" ]]; then
    exit 0
fi

# Interval between in-session checkpoints (set 0 to disable throttling).
THROTTLE_SECONDS="${OM_SESSION_OBSERVER_INTERVAL_SECONDS:-900}"
if ! [[ "$THROTTLE_SECONDS" =~ ^[0-9]+$ ]]; then
    THROTTLE_SECONDS=900
fi

if ! [[ "$LOCK_STALE_MINUTES" =~ ^[0-9]+$ ]]; then
    LOCK_STALE_MINUTES=60
fi

count_session_messages() {
    local transcript_path=$1
    local count=""

    if jq empty "$transcript_path" >/dev/null 2>&1; then
        count="$(jq -r '
            def is_message:
                (.type == "user")
                or (.type == "assistant")
                or (.role == "user")
                or (.role == "assistant")
                or ((.message | type) == "object"
                    and ((.message.role == "user") or (.message.role == "assistant")));
            if type == "array" then
                [ .[] | select(type == "object") | select(is_message) ] | length
            elif type == "object" then
                if (.items | type) == "array" then
                    [ .items[] | select(type == "object") | select(is_message) ] | length
                else
                    (if is_message then 1 else 0 end)
                end
            else
                0
            end
        ' "$transcript_path" 2>/dev/null || true)"
    fi

    if [[ -n "$count" ]] && [[ "$count" =~ ^[0-9]+$ ]]; then
        echo "$count"
        return
    fi

    jq -R '
        fromjson? as $entry
        | if $entry == null then
            empty
        elif ($entry.type == "user" or $entry.type == "assistant") then
            1
        elif (($entry.message | type == "object")
            and (($entry.message.role == "user") or ($entry.message.role == "assistant"))) then
            1
        elif ($entry.role == "user" or $entry.role == "assistant") then
            1
        else
            empty
        end
    ' "$transcript_path" | wc -l | tr -d " "
}

state_read_field() {
    local field=$1
    if [[ ! -f "$STATE_FILE" ]] || ! jq empty "$STATE_FILE" >/dev/null 2>&1; then
        echo ""
        return
    fi
    jq -r --arg p "$TRANSCRIPT" --arg field "$field" '.[$p][$field] // empty' "$STATE_FILE"
}

state_message_count() {
    local count
    count="$(state_read_field "message_count")"
    if [[ -z "$count" ]]; then
        # Backward compatibility with older state entries.
        count="$(state_read_field "line_count")"
    fi
    echo "$count"
}

write_state() {
    local now_ts=$1
    local message_count=$2
    local status=$3
    local state_tmp
    state_tmp="$(mktemp)"

    jq -n --arg p "$TRANSCRIPT" --argjson now_ts "$now_ts" --argjson message_count "$message_count" --arg status "$status" \
        '.[$p] = {last_observed: $now_ts, message_count: $message_count, status: $status}' \
        < /dev/null > "$state_tmp"

    if [[ -s "$STATE_FILE" ]] && jq empty "$STATE_FILE" >/dev/null 2>&1; then
        jq --arg p "$TRANSCRIPT" --argjson now_ts "$now_ts" --argjson message_count "$message_count" --arg status "$status" \
            '.[$p] = {last_observed: $now_ts, message_count: $message_count, status: $status}' \
            "$STATE_FILE" > "$state_tmp"
    fi

    mv "$state_tmp" "$STATE_FILE"
}

should_skip_observer() {
    local now_ts=$1
    local message_count=$2
    local last_message_count=$3
    local last_observed_at=$4

    if [[ -n "$last_message_count" ]] && (( message_count <= last_message_count )); then
        return 0
    fi

    if [[ -n "$last_observed_at" ]] && (( now_ts - last_observed_at < THROTTLE_SECONDS )); then
        return 0
    fi

    return 1
}

acquire_lock() {
    local lock_path=$1
    mkdir -p "$LOCK_DIR"
    # Best-effort stale lock cleanup so interrupted hooks do not block forever.
    if [[ "$LOCK_STALE_MINUTES" -gt 0 ]]; then
        find "$LOCK_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +"$LOCK_STALE_MINUTES" -exec rm -rf {} + 2>/dev/null || true
    fi

    if mkdir "$lock_path" 2>/dev/null; then
        return 0
    fi

    if [[ "$LOCK_STALE_MINUTES" -gt 0 ]] && [[ -n "$(find "$lock_path" -prune -mmin +"$LOCK_STALE_MINUTES" -print -quit 2>/dev/null || true)" ]]; then
        rm -rf "$lock_path" 2>/dev/null || true
        if mkdir "$lock_path" 2>/dev/null; then
            return 0
        fi
    fi

    return 1
}

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

# Early exit for disabled checkpoints (cheap check, no lock needed).
if [[ "$is_force_event" == false ]] && [[ "$is_checkpoint_event" == true ]]; then
    case "$(printf '%s' "$DISABLE_CHECKPOINTS" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            exit 0
            ;;
    esac
fi

# Acquire lock BEFORE the throttle check so concurrent hooks cannot all pass
# the throttle with stale state and then race to start duplicate observers.
sanitized_transcript="${TRANSCRIPT//[\/:.]/_}"
lock_path="$LOCK_DIR/$sanitized_transcript"
if ! acquire_lock "$lock_path"; then
    exit 0
fi

# Throttle check (with lock held). Force events skip throttling entirely.
if [[ "$is_force_event" == false ]] && [[ "$THROTTLE_SECONDS" -gt 0 ]]; then
    now=$(date +%s)
    last_message_count="$(state_message_count)"
    last_observed_at="$(state_read_field "last_observed")"
    transcript_messages="$(count_session_messages "$TRANSCRIPT")"

    if should_skip_observer "$now" "$transcript_messages" "$last_message_count" "$last_observed_at"; then
        write_state "$now" "$transcript_messages" "skipped"
        rm -rf "$lock_path"
        exit 0
    fi
fi

now=$(date +%s)
transcript_messages="$(count_session_messages "$TRANSCRIPT")"
write_state "$now" "$transcript_messages" "in_progress"

# Run observer in background so we don't block session lifecycle.
(
    trap 'rm -rf "$lock_path"' EXIT
    "$OM" observe --transcript "$TRANSCRIPT" --source claude
    observe_status=$?

    now=$(date +%s)
    transcript_messages="$(count_session_messages "$TRANSCRIPT")"
    if [[ $observe_status -eq 0 ]]; then
        write_state "$now" "$transcript_messages" "success"
    else
        echo "Warning: om observe failed for $TRANSCRIPT with status $observe_status" >&2
        write_state "$now" "$transcript_messages" "failed"
    fi
) &
disown

exit 0
