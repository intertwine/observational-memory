"""Observer: compress transcripts into observations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .llm import compress
from .transcripts import Message

OBSERVER_PROMPT_PATH = Path(__file__).parent / "prompts" / "observer.md"


def _recent_observations_window(observations: str, config: Config) -> str:
    """Return only the most recent tail of observations for dedup context.

    Bounds the observer's input so it doesn't re-send the entire (growing)
    observations.md on every run. A header note tells the model the older
    history was elided. ``observer_context_max_chars <= 0`` disables the cap.
    """
    cap = config.observer_context_max_chars
    if cap <= 0 or len(observations) <= cap:
        return observations
    tail = observations[-cap:]
    # Start at the next record/section boundary so we don't begin mid-entry.
    for marker in ("\n## ", "\n### ", "\n\n"):
        idx = tail.find(marker)
        if idx != -1:
            tail = tail[idx + 1 :]
            break
    return f"<!-- Older observations elided for context budget; showing the most recent ~{cap} chars. -->\n\n{tail}"


def run_observer(
    messages: list[Message],
    config: Config | None = None,
    dry_run: bool = False,
    *,
    transcript_path: Path | None = None,
    source: str | None = None,
) -> str | None:
    """Compress a list of messages into observations and append to observations.md.

    Args:
        messages: Normalized messages to compress.
        config: Runtime config. Uses defaults if None.
        dry_run: If True, return the new observations without writing.

    Returns:
        The new observations text, or None if below threshold.
    """
    if config is None:
        config = Config()

    if len(messages) < config.min_messages:
        return None

    system_prompt = _load_observer_prompt()
    transcript_text = _format_messages(messages)

    # The observer prompt asks for the *complete* observations.md. In
    # non-cluster mode `_write_observations` overwrites the file with that
    # output, so truncating the input here would silently drop older days.
    # Cluster mode is safe: `_write_observation_record` appends a new record and
    # `materialize._render_observations` rebuilds the view from the full,
    # append-only record log — older observations live in older records and are
    # never lost. So we only bound the dedup context in the append-only path.
    cluster_mode = _cluster_enabled(config)
    existing_observations = ""
    if config.observations_path.exists():
        existing_observations = config.observations_path.read_text()
        if cluster_mode:
            existing_observations = _recent_observations_window(existing_observations, config)

    user_content = (
        f"## Existing observations\n\n{existing_observations}\n\n"
        f"---\n\n"
        f"## New transcript to process\n\n{transcript_text}"
    )

    result = compress(system_prompt, user_content, config, operation="observer")

    if dry_run:
        return result

    if cluster_mode:
        _write_observation_record(result, messages, config, transcript_path=transcript_path, source=source)
        return result

    _write_observations(result, config)
    return result


def observe_claude_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Claude Code transcript."""
    from .transcripts.claude import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_uuid = cursor.get(cursor_key)

    messages = parse_transcript(transcript_path, after_uuid=after_uuid)

    if not messages:
        return None

    result = run_observer(messages, config, dry_run, transcript_path=transcript_path, source="claude")

    if result and not dry_run:
        # Update cursor to last message UUID — find it from the transcript
        import json

        last_uuid = None
        for line in reversed(transcript_path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") in ("user", "assistant") and entry.get("uuid"):
                    last_uuid = entry["uuid"]
                    break
            except json.JSONDecodeError:
                continue
        if last_uuid:
            cursor[cursor_key] = last_uuid
            config.save_cursor(cursor)

    return result


def observe_all_claude(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Claude Code transcripts and run observer on each."""
    from .transcripts.claude import find_recent_transcripts

    if config is None:
        config = Config()

    results = []
    for path in find_recent_transcripts(config.claude_projects_dir):
        result = observe_claude_transcript(path, config, dry_run)
        if result:
            results.append(result)
    return results


def observe_cowork_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Cowork audit.jsonl transcript."""
    from .transcripts.claude import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_uuid = cursor.get(cursor_key)

    messages = parse_transcript(transcript_path, after_uuid=after_uuid, source="cowork")

    if not messages:
        return None

    result = run_observer(messages, config, dry_run, transcript_path=transcript_path, source="cowork")

    if result and not dry_run:
        import json

        last_uuid = None
        for line in reversed(transcript_path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") in ("user", "assistant") and entry.get("uuid"):
                    last_uuid = entry["uuid"]
                    break
            except json.JSONDecodeError:
                continue
        if last_uuid:
            cursor[cursor_key] = last_uuid
            config.save_cursor(cursor)

    return result


def observe_all_cowork(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Cowork session transcripts and run observer on each."""
    from .transcripts.cowork import find_recent_transcripts

    if config is None:
        config = Config()

    results = []
    for path in find_recent_transcripts(config.cowork_sessions_dir):
        result = observe_cowork_transcript(path, config, dry_run)
        if result:
            results.append(result)
    return results


def observe_aside_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Aside ``messages.jsonl`` transcript.

    Aside records carry no per-message UUID, so resumption is count-based
    (matching Codex/Grok): the cursor stores the number of user/assistant
    messages already observed for this transcript path.
    """
    from .transcripts.aside import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_index = cursor.get(cursor_key)
    if not isinstance(after_index, int):
        after_index = 0

    all_messages = parse_transcript(transcript_path, source="aside")
    if not all_messages:
        return None

    if after_index > len(all_messages):
        # Transcript was rotated/truncated; reprocess from the start safely.
        after_index = 0

    messages = all_messages[after_index:]
    if not messages:
        return None

    result = run_observer(messages, config, dry_run, transcript_path=transcript_path, source="aside")

    if result and not dry_run:
        cursor[cursor_key] = len(all_messages)
        config.save_cursor(cursor)

    return result


def observe_all_aside(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Aside session transcripts and run observer on each."""
    from .transcripts.aside import find_recent_transcripts

    if config is None:
        config = Config()

    results = []
    for path in find_recent_transcripts(config.aside_home):
        result = observe_aside_transcript(path, config, dry_run)
        if result:
            results.append(result)
    return results


def observe_auto_memory(config: Config | None = None, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Scan Claude Code auto-memory files and update the search index.

    Unlike transcript observers, this does NOT call the LLM — auto-memory
    files are already distilled facts. It detects changed files via content
    hashing and triggers a reindex when changes are found.

    Args:
        config: Runtime config. Uses defaults if None.
        dry_run: If True, report changes without updating cursor or index.

    Returns:
        Tuple of (changed_paths, deleted_paths).
    """
    from .transcripts.auto_memory import (
        detect_changes,
        find_memory_directories,
        scan_memory_files,
        update_cursor,
    )

    if config is None:
        config = Config()

    # Collect all memory files across projects
    all_files = []
    for memory_dir in find_memory_directories(config.claude_projects_dir):
        all_files.extend(scan_memory_files(memory_dir))

    # Detect changes against cursor (even when all_files is empty —
    # deletions of the last file must still clear stale index entries)
    cursor = config.load_cursor()
    amem_cursor = cursor.get("claude-memory", {})
    changed, deleted = detect_changes(amem_cursor, all_files)

    if not changed and not deleted:
        return [], []

    changed_paths = [str(mf.path) for mf in changed]

    if not dry_run:
        update_cursor(cursor, all_files)
        config.save_cursor(cursor)
        _reindex_if_enabled(config)

    return changed_paths, deleted


def observe_hermes_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Hermes session JSONL file.

    Parses the Hermes session log into normalized messages (filtering out
    tool outputs, session_meta, and machine-oriented records), then runs
    the observer LLM to extract observations.
    """
    from .transcripts.hermes import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_index = cursor.get(cursor_key)
    if not isinstance(after_index, int):
        after_index = 0

    messages = parse_transcript(transcript_path, after_index=after_index)
    if not messages:
        return None

    result = run_observer(messages, config, dry_run, transcript_path=transcript_path, source="hermes")
    if result and not dry_run:
        cursor[cursor_key] = after_index + len(messages)
        config.save_cursor(cursor)

    return result


def observe_all_hermes(
    sessions_dir: Path | None = None,
    config: Config | None = None,
    dry_run: bool = False,
    max_age_hours: int = 24,
) -> list[str]:
    """Scan recent Hermes session logs and run observer on each.

    Args:
        sessions_dir: Path to Hermes sessions directory
            (default: ~/.hermes/sessions).
        config: Runtime config. Uses defaults if None.
        dry_run: If True, return observations without writing.
        max_age_hours: Only process sessions modified within this window.

    Returns:
        List of observation texts produced.
    """
    from .transcripts.hermes import find_recent_sessions

    if config is None:
        config = Config()

    if sessions_dir is None:
        sessions_dir = config.hermes_sessions_dir

    results = []
    for path in find_recent_sessions(sessions_dir, max_age_hours=max_age_hours):
        result = observe_hermes_transcript(path, config, dry_run)
        if result:
            results.append(result)
    return results


def observe_codex_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Codex transcript."""
    if config is None:
        config = Config()

    cursor = config.load_cursor()
    messages, total_messages = _codex_messages_since_cursor(transcript_path, cursor)
    if not messages:
        return None

    result = run_observer(messages, config, dry_run, transcript_path=transcript_path, source="codex")
    if result and not dry_run:
        cursor[str(transcript_path)] = total_messages
        config.save_cursor(cursor)

    return result


def observe_all_codex(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Codex sessions and run observer on each."""
    from .transcripts.codex import find_recent_sessions

    if config is None:
        config = Config()

    results = []

    for path in find_recent_sessions(config.codex_home):
        result = observe_codex_transcript(path, config, dry_run)
        if result:
            results.append(result)

    return results


def _codex_messages_since_cursor(transcript_path: Path, cursor: dict) -> tuple[list[Message], int]:
    """Return new Codex messages for a transcript plus the total parsed count."""
    from .transcripts.codex import line_offset_to_message_count, parse_transcript

    cursor_key = str(transcript_path)
    after_index = cursor.get(cursor_key)
    if not isinstance(after_index, int):
        after_index = 0

    all_messages = parse_transcript(transcript_path)
    if not all_messages:
        return [], 0

    if after_index and after_index > len(all_messages):
        # Backward compatibility: older cursors tracked raw JSONL line offsets
        # rather than parsed message counts. Convert by counting how many of the
        # first N file lines are actual messages. If the converted index is still
        # out of range (common when non-message records inflate line counts),
        # fall back to 0 so we safely reprocess rather than skip messages.
        migrated_index = line_offset_to_message_count(transcript_path, after_index)
        if 0 <= migrated_index < len(all_messages):
            after_index = migrated_index
        else:
            after_index = 0

    return all_messages[after_index:], len(all_messages)


def _chunk_messages(messages: list[Message], chunk_size: int = 200) -> list[list[Message]]:
    """Split a list of Message objects into chunks of at most *chunk_size*.

    Returns:
        A list of lists, each containing up to *chunk_size* messages.
        Returns an empty list if *messages* is empty.
    """
    if not messages:
        return []
    return [messages[i : i + chunk_size] for i in range(0, len(messages), chunk_size)]


def run_observer_backfill(
    messages: list[Message],
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Compress messages into observations in backfill mode.

    Unlike :func:`run_observer`, this does **not** include existing observations
    in the LLM prompt — keeping context small when replaying large histories.
    The resulting observations are **appended** to ``observations.md`` rather
    than overwriting it.

    Args:
        messages: Normalized messages to compress.
        config: Runtime config. Uses defaults if None.
        dry_run: If True, return the new observations without writing.

    Returns:
        The new observations text, or None if below threshold.
    """
    if config is None:
        config = Config()

    if len(messages) < config.min_messages:
        return None

    system_prompt = _load_observer_prompt()
    transcript_text = _format_messages(messages)

    user_content = f"## New transcript to process\n\n{transcript_text}"

    result = compress(system_prompt, user_content, config, operation="observer")

    if dry_run:
        return result

    if _cluster_enabled(config):
        _write_observation_record(result, messages, config, source="backfill")
        return result

    _append_observations(result, config, skip_reindex=True)
    return result


def observe_claude_transcript_backfill(
    transcript_path: Path,
    config: Config | None = None,
    chunk_size: int = 200,
    dry_run: bool = False,
) -> int | None:
    """Process a single Claude Code transcript in backfill mode.

    1. Load cursor and get the ``after_uuid`` for *transcript_path*.
    2. Parse the transcript incrementally (using the cursor).
    3. If no messages, return ``None``.
    4. If the number of messages exceeds *chunk_size*, split into chunks.
    5. Process each chunk through :func:`run_observer_backfill`.
    6. Update the cursor to the last UUID in the transcript.
    7. Return the total number of characters written.

    Args:
        transcript_path: Path to a Claude Code ``.jsonl`` transcript.
        config: Runtime config. Uses defaults if None.
        chunk_size: Maximum messages per LLM call.
        dry_run: If True, skip writing observations and cursor updates.

    Returns:
        Total characters of observation text produced, or ``None`` if the
        transcript had no new messages.
    """
    from .transcripts.claude import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_uuid = cursor.get(cursor_key)

    messages = parse_transcript(transcript_path, after_uuid=after_uuid)

    if not messages:
        return None

    return _backfill_from_messages(messages, transcript_path, config, chunk_size, dry_run)


def observe_cowork_transcript_backfill(
    transcript_path: Path,
    config: Config | None = None,
    chunk_size: int = 200,
    dry_run: bool = False,
) -> int | None:
    """Process a single Cowork audit.jsonl transcript in backfill mode.

    Same logic as :func:`observe_claude_transcript_backfill` but parses
    with ``source="cowork"``.
    """
    from .transcripts.claude import parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    after_uuid = cursor.get(cursor_key)

    messages = parse_transcript(transcript_path, after_uuid=after_uuid, source="cowork")

    if not messages:
        return None

    return _backfill_from_messages(messages, transcript_path, config, chunk_size, dry_run)


def _backfill_from_messages(
    messages: list[Message],
    transcript_path: Path,
    config: Config,
    chunk_size: int,
    dry_run: bool,
) -> int:
    """Shared backfill logic: chunk, observe, update cursor."""
    chunks = _chunk_messages(messages, chunk_size)
    total_chars = 0

    for chunk in chunks:
        result = run_observer_backfill(chunk, config, dry_run)
        if result:
            total_chars += len(result)

    if not dry_run:
        # Update cursor to the last message UUID in the transcript
        import json

        cursor = config.load_cursor()
        cursor_key = str(transcript_path)
        last_uuid = None
        for line in reversed(transcript_path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") in ("user", "assistant") and entry.get("uuid"):
                    last_uuid = entry["uuid"]
                    break
            except json.JSONDecodeError:
                continue
        if last_uuid:
            cursor[cursor_key] = last_uuid
            config.save_cursor(cursor)
        # Reindex once after all chunks are written
        _reindex_if_enabled(config)

    return total_chars


def _append_observations(new_observations: str, config: Config, *, skip_reindex: bool = False) -> None:
    """Append new observations to the observations file (never overwrite)."""
    from .startup_memory import refresh_startup_memory

    config.ensure_memory_dir()
    if config.observations_path.exists():
        existing = config.observations_path.read_text()
        config.observations_path.write_text(existing.rstrip() + "\n\n" + new_observations.rstrip() + "\n")
    else:
        config.observations_path.write_text(new_observations.rstrip() + "\n")
    refresh_startup_memory(config)
    if not skip_reindex:
        _reindex_if_enabled(config)


def _load_observer_prompt() -> str:
    """Load the observer system prompt."""
    if OBSERVER_PROMPT_PATH.exists():
        return OBSERVER_PROMPT_PATH.read_text()
    # Fallback minimal prompt
    return (
        "You are the Observer. Compress the following conversation transcript into "
        "dense, prioritized observation notes using the 🔴/🟡/🟢 priority system. "
        "Output only the new observations section in markdown format."
    )


def _format_messages(messages: list[Message]) -> str:
    """Format messages into a readable transcript for the LLM."""
    lines = []
    for msg in messages:
        ts = msg.timestamp[:19] if msg.timestamp else "??:??"
        prefix = "USER" if msg.role == "user" else "ASSISTANT"
        source_tag = f"[{msg.source}]" if msg.source else ""
        lines.append(f"[{ts}] {prefix} {source_tag}: {msg.content}")
    return "\n\n".join(lines)


def _reindex_if_enabled(config: Config) -> None:
    """Silently rebuild the search index after memory writes."""
    if config.search_backend == "none":
        return
    try:
        from .search import reindex

        reindex(config)
    except Exception:
        pass  # Never block observe/reflect on search failures


def _cluster_enabled(config: Config) -> bool:
    try:
        from .sync import cluster_feature_enabled

        return cluster_feature_enabled(config)
    except Exception:
        return False


def _write_observation_record(
    result: str,
    messages: list[Message],
    config: Config,
    *,
    transcript_path: Path | None = None,
    source: str | None = None,
) -> None:
    from .sync.config import load_cluster_config
    from .sync.engine import sync_cluster
    from .sync.materialize import materialize_cluster_memory
    from .sync.source import namespace_for_event, source_metadata
    from .sync.store import ClusterStore

    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        _write_observations(result, config)
        return

    store = ClusterStore.from_config(config)
    source_event = source_metadata(
        config=config,
        cluster_config=cluster_config,
        messages=messages,
        source=source,
        transcript_path=transcript_path,
    )
    observed_at = _latest_message_timestamp(messages) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.append_record(
        kind="observation",
        namespace=namespace_for_event(cluster_config, source_event),
        source=source_event,
        payload={
            "format": "markdown",
            "body": result,
            "observed_at": observed_at,
            "message_count": len(messages),
            "retention": "recent",
        },
    )
    materialize_cluster_memory(config, store)
    if cluster_config.sync_on_observe:
        try:
            sync_cluster(config, deadline_ms=1500)
        except Exception:
            pass


def _latest_message_timestamp(messages: list[Message]) -> str | None:
    timestamps = [message.timestamp for message in messages if message.timestamp]
    return max(timestamps) if timestamps else None


def _write_observations(new_observations: str, config: Config) -> None:
    """Write or append observations to the file."""
    from .startup_memory import refresh_startup_memory

    config.ensure_memory_dir()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if config.observations_path.exists():
        existing = config.observations_path.read_text()
        # The LLM returns the full updated observations — just write it
        if existing.strip() and f"## {today}" in new_observations:
            config.observations_path.write_text(new_observations.rstrip() + "\n")
        else:
            # Append if the LLM only returned the new section
            config.observations_path.write_text(existing.rstrip() + "\n\n" + new_observations.rstrip() + "\n")
    else:
        config.observations_path.write_text(new_observations.rstrip() + "\n")
    refresh_startup_memory(config)
    _reindex_if_enabled(config)


# --- Grok observation support (Phase 2) ---


def observe_grok_transcript(
    transcript_path: Path,
    config: Config | None = None,
    dry_run: bool = False,
) -> str | None:
    """Run observer on a specific Grok updates.jsonl transcript.

    Grok sessions can be very long (streaming chunks). We split into manageable
    batches (max 250 messages per LLM call) to avoid empty responses from the
    observer model on oversized prompts. Cursor is updated progressively.
    """
    if config is None:
        config = Config()

    from .transcripts.grok import parse_transcript

    cursor = config.load_cursor()
    cursor_key = str(transcript_path)
    all_parsed_messages = parse_transcript(transcript_path, source="grok")
    if not all_parsed_messages:
        return None

    after_count = cursor.get(cursor_key)
    after_index = int(after_count) if isinstance(after_count, (int, str)) and str(after_count).isdigit() else 0
    if after_index > len(all_parsed_messages):
        after_index = 0

    all_messages = all_parsed_messages[after_index:]
    if not all_messages:
        return None

    # Chunk large transcripts (Grok-specific robustness for long agent sessions)
    MAX_BATCH = 250
    results = []
    for i in range(0, len(all_messages), MAX_BATCH):
        batch = all_messages[i : i + MAX_BATCH]
        res = run_observer(batch, config, dry_run, transcript_path=transcript_path, source="grok")
        if res:
            results.append(res)

    combined = "\n\n".join(r for r in results if r) if results else None

    if not dry_run and (combined or len(all_messages) >= config.min_messages):
        # Use count-based cursor for Grok to avoid reprocessing same-second chunks
        cursor[cursor_key] = len(all_parsed_messages)
        config.save_cursor(cursor)

    return combined


def observe_all_grok(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Grok sessions and run observer on new ones."""
    from .transcripts.grok import find_recent_grok_sessions

    if config is None:
        config = Config()

    results = []

    for path in find_recent_grok_sessions(config.grok_sessions_dir):
        result = observe_grok_transcript(path, config, dry_run)
        if result:
            results.append(result)

    return results
