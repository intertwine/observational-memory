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
    from .transcripts.codex import find_recent_sessions, parse_transcript

    if config is None:
        config = Config()

    cursor = config.load_cursor()
    results = []

    for path in find_recent_sessions(config.codex_home):
        cursor_key = str(path)
        after_index = cursor.get(cursor_key)

        messages = parse_transcript(path, after_index=after_index)
        if not messages:
            continue

        result = run_observer(messages, config, dry_run)
        if result and not dry_run:
            # Count total lines for cursor
            total_lines = len(path.read_text().splitlines())
            cursor[cursor_key] = total_lines
            config.save_cursor(cursor)

        if result:
            results.append(result)

    return results


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
