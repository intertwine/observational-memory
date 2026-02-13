"""Observer: compress transcripts into observations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .llm import compress
from .transcripts import Message


OBSERVER_PROMPT_PATH = Path(__file__).parent / "prompts" / "observer.md"


def run_observer(
    messages: list[Message],
    config: Config | None = None,
    dry_run: bool = False,
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

    existing_observations = ""
    if config.observations_path.exists():
        existing_observations = config.observations_path.read_text()

    user_content = (
        f"## Existing observations\n\n{existing_observations}\n\n"
        f"---\n\n"
        f"## New transcript to process\n\n{transcript_text}"
    )

    result = compress(system_prompt, user_content, config)

    if dry_run:
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

    result = run_observer(messages, config, dry_run)

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


def observe_all_codex(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan all recent Codex sessions and run observer on each."""
    from .transcripts.codex import (
        find_recent_sessions,
        line_offset_to_message_count,
        parse_transcript,
    )

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    results = []

    for path in find_recent_sessions(config.codex_home):
        cursor_key = str(path)
        after_index = cursor.get(cursor_key)
        if not isinstance(after_index, int):
            after_index = 0

        all_messages = parse_transcript(path)
        if not all_messages:
            continue

        if after_index and after_index > len(all_messages):
            # Backward compatibility: older cursors tracked raw JSONL line offsets
            # rather than parsed message counts. Convert by counting how many of the
            # first N file lines are actual messages. If the converted index is still
            # out of range (common when non-message records inflate line counts),
            # fall back to 0 so we safely reprocess rather than skip messages.
            migrated_index = line_offset_to_message_count(path, after_index)
            if 0 <= migrated_index < len(all_messages):
                after_index = migrated_index
            else:
                after_index = 0

        messages = all_messages[after_index:]
        if not messages:
            continue

        result = run_observer(messages, config, dry_run)
        if result and not dry_run:
            # Track processed message count for incremental parsing.
            cursor[cursor_key] = len(all_messages)
            config.save_cursor(cursor)

        if result:
            results.append(result)

    return results


def _chunk_messages(
    messages: list[Message], chunk_size: int = 200
) -> list[list[Message]]:
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

    result = compress(system_prompt, user_content, config)

    if dry_run:
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

    chunks = _chunk_messages(messages, chunk_size)
    total_chars = 0

    for chunk in chunks:
        result = run_observer_backfill(chunk, config, dry_run)
        if result:
            total_chars += len(result)

    if not dry_run:
        # Update cursor to the last message UUID in the transcript
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
        # Reindex once after all chunks are written
        _reindex_if_enabled(config)

    return total_chars


def _append_observations(new_observations: str, config: Config, *, skip_reindex: bool = False) -> None:
    """Append new observations to the observations file (never overwrite)."""
    config.ensure_memory_dir()
    if config.observations_path.exists():
        existing = config.observations_path.read_text()
        config.observations_path.write_text(
            existing.rstrip() + "\n\n" + new_observations.rstrip() + "\n"
        )
    else:
        config.observations_path.write_text(new_observations.rstrip() + "\n")
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


def _write_observations(new_observations: str, config: Config) -> None:
    """Write or append observations to the file."""
    config.ensure_memory_dir()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if config.observations_path.exists():
        existing = config.observations_path.read_text()
        # The LLM returns the full updated observations — just write it
        if existing.strip() and f"## {today}" in new_observations:
            config.observations_path.write_text(new_observations.rstrip() + "\n")
        else:
            # Append if the LLM only returned the new section
            config.observations_path.write_text(
                existing.rstrip() + "\n\n" + new_observations.rstrip() + "\n"
            )
    else:
        config.observations_path.write_text(new_observations.rstrip() + "\n")
    _reindex_if_enabled(config)
