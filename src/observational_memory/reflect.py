"""Reflector: condense observations into long-term reflections."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .config import Config
from .llm import compress
from pathlib import Path


REFLECTOR_PROMPT_PATH = Path(__file__).parent / "prompts" / "reflector.md"

# Approximate chars-per-token ratio for estimating input size.
_CHARS_PER_TOKEN = 3.5
# Maximum input tokens to send in a single reflector call.
_MAX_INPUT_TOKENS = 30_000
# max_tokens for reflector output (200-600 lines needs room)
_REFLECTOR_MAX_OUTPUT_TOKENS = 8192

# Regex for the "Last reflected" timestamp line in reflections.md
_LAST_REFLECTED_RE = re.compile(
    r"^\*Last reflected:\s*(\d{4}-\d{2}-\d{2})\b.*\*$", re.MULTILINE
)
# Regex for the "Last updated" timestamp line
_LAST_UPDATED_RE = re.compile(
    r"^\*Last updated:.*\*$", re.MULTILINE
)


def run_reflector(config: Config | None = None, dry_run: bool = False) -> str | None:
    """Read observations + reflections, condense, write updated reflections.

    Only processes observations newer than the ``Last reflected`` timestamp
    in the existing reflections. When those observations are small enough,
    processes in a single LLM call. When they are large, chunks them by
    date section and folds each chunk into the reflections incrementally.

    Args:
        config: Runtime config.
        dry_run: If True, return result without writing.

    Returns:
        The new reflections text, or None if nothing to reflect on.
    """
    if config is None:
        config = Config()

    raw_observations = ""
    if config.observations_path.exists():
        raw_observations = config.observations_path.read_text()

    if not raw_observations.strip():
        return None

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

    # Filter to only new observations since last reflection
    last_reflected_date = _parse_last_reflected(reflections)
    observations = _filter_new_observations(raw_observations, last_reflected_date)

    if not observations.strip():
        return None

    system_prompt = _load_reflector_prompt()

    # Estimate total input size
    total_input_chars = len(system_prompt) + len(reflections) + len(observations)
    estimated_tokens = total_input_chars / _CHARS_PER_TOKEN

    if estimated_tokens <= _MAX_INPUT_TOKENS:
        # Small enough — single pass
        result = _reflect_single(system_prompt, reflections, observations, config)
    else:
        # Too large — chunk observations and fold incrementally
        result = _reflect_chunked(system_prompt, reflections, observations, config)

    # Programmatically stamp the "Last reflected" timestamp so we don't
    # rely on the LLM to format it correctly.
    latest_obs_date = _extract_latest_observation_date(raw_observations)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    result = _stamp_timestamps(result, now_utc, latest_obs_date or now_utc)

    if dry_run:
        return result

    _write_reflections(result, config)
    _trim_old_observations(config)
    _reindex_if_enabled(config)

    return result


def _reflect_single(
    system_prompt: str, reflections: str, observations: str, config: Config
) -> str:
    """Single-pass reflection for small observation sets."""
    user_content = (
        f"## Current reflections\n\n{reflections}\n\n"
        f"---\n\n"
        f"## Current observations\n\n{observations}"
    )
    return compress(system_prompt, user_content, config, max_tokens=_REFLECTOR_MAX_OUTPUT_TOKENS)


def _reflect_chunked(
    system_prompt: str, reflections: str, observations: str, config: Config
) -> str:
    """Chunked reflection: split observations into date sections, fold each into reflections."""
    chunks = _chunk_observations(observations)

    running_reflections = reflections

    for i, chunk in enumerate(chunks, 1):
        is_last = i == len(chunks)
        fold_prompt = system_prompt
        if not is_last:
            # For intermediate chunks, tell the model more data is coming
            fold_prompt += (
                "\n\n**NOTE:** This is chunk {i} of {total}. More observations follow. "
                "Focus on integrating these observations into the reflections. "
                "Produce the complete updated reflections document."
            ).format(i=i, total=len(chunks))

        user_content = (
            f"## Current reflections\n\n{running_reflections}\n\n"
            f"---\n\n"
            f"## Observations (chunk {i}/{len(chunks)})\n\n{chunk}"
        )
        running_reflections = compress(
            fold_prompt, user_content, config, max_tokens=_REFLECTOR_MAX_OUTPUT_TOKENS
        )

    return running_reflections


def _chunk_observations(observations: str) -> list[str]:
    """Split observations by date headers into chunks that fit within token limits.

    Groups consecutive date sections until adding another would exceed the
    per-chunk token budget. Each chunk is a string of one or more date sections.
    """
    # Budget per chunk: leave room for reflections + system prompt
    budget_chars = int(_MAX_INPUT_TOKENS * _CHARS_PER_TOKEN * 0.6)

    # Split by date headers, keeping each "## YYYY-MM-DD" section together
    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", observations, flags=re.MULTILINE)

    # First element may be the "# Observations" header — prepend to first date section
    header = ""
    date_sections = []
    for section in sections:
        if re.match(r"## \d{4}-\d{2}-\d{2}", section.strip()):
            date_sections.append(section)
        else:
            header = section

    if not date_sections:
        # No date sections found — return as single chunk
        return [observations]

    chunks: list[str] = []
    current_chunk = header
    for section in date_sections:
        if len(current_chunk) + len(section) > budget_chars and current_chunk.strip():
            chunks.append(current_chunk)
            current_chunk = header  # restart with header for context
        current_chunk += section

    if current_chunk.strip():
        chunks.append(current_chunk)

    return chunks if chunks else [observations]


def _parse_last_reflected(reflections: str) -> str | None:
    """Extract the ``Last reflected`` date from reflections.md.

    Returns:
        A ``YYYY-MM-DD`` string, or None if not found.
    """
    m = _LAST_REFLECTED_RE.search(reflections)
    return m.group(1) if m else None


def _filter_new_observations(observations: str, since_date: str | None) -> str:
    """Return only observation sections from *since_date* onward (inclusive).

    If *since_date* is None, returns the full observations text (first run).
    Includes the file header (``# Observations`` etc.) in the output.
    """
    if since_date is None:
        return observations

    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", observations, flags=re.MULTILINE)

    header = ""
    kept: list[str] = []
    for section in sections:
        date_match = re.match(r"## (\d{4}-\d{2}-\d{2})", section.strip())
        if date_match:
            if date_match.group(1) >= since_date:
                kept.append(section)
        else:
            header = section

    if not kept:
        return ""

    return header + "".join(kept)


def _extract_latest_observation_date(observations: str) -> str | None:
    """Find the most recent ``## YYYY-MM-DD`` date in observations.

    Returns:
        A ``YYYY-MM-DD`` string, or None if no date headers found.
    """
    dates = re.findall(r"^## (\d{4}-\d{2}-\d{2})", observations, flags=re.MULTILINE)
    return max(dates) if dates else None


def _stamp_timestamps(reflections: str, updated: str, reflected: str) -> str:
    """Ensure reflections have correct ``Last updated`` and ``Last reflected`` lines.

    Injects or replaces the timestamps programmatically so we don't rely on
    the LLM to format them correctly.
    """
    updated_line = f"*Last updated: {updated}*"
    reflected_line = f"*Last reflected: {reflected}*"

    has_updated = _LAST_UPDATED_RE.search(reflections)
    has_reflected = _LAST_REFLECTED_RE.search(reflections)

    if has_updated:
        reflections = _LAST_UPDATED_RE.sub(updated_line, reflections, count=1)
    if has_reflected:
        reflections = _LAST_REFLECTED_RE.sub(reflected_line, reflections, count=1)

    # If "Last reflected" wasn't in the LLM output, insert it after "Last updated"
    if not has_reflected:
        if has_updated or _LAST_UPDATED_RE.search(reflections):
            reflections = _LAST_UPDATED_RE.sub(
                f"{updated_line}\n{reflected_line}", reflections, count=1
            )
        else:
            # No timestamp lines at all — insert after the title
            title_match = re.match(r"(#[^\n]*\n)", reflections)
            if title_match:
                insert_pos = title_match.end()
                reflections = (
                    reflections[:insert_pos]
                    + f"\n{updated_line}\n{reflected_line}\n"
                    + reflections[insert_pos:]
                )

    return reflections


def _reindex_if_enabled(config: Config) -> None:
    """Silently rebuild the search index after memory writes."""
    if config.search_backend == "none":
        return
    try:
        from .search import reindex
        reindex(config)
    except Exception:
        pass  # Never block observe/reflect on search failures


def _load_reflector_prompt() -> str:
    """Load the reflector system prompt."""
    if REFLECTOR_PROMPT_PATH.exists():
        return REFLECTOR_PROMPT_PATH.read_text()
    return (
        "You are the Reflector. Condense the observations into a stable long-term "
        "memory document. Merge, promote (🟡→🔴), demote, and archive entries. "
        "Output the complete reflections.md content."
    )


def _write_reflections(reflections: str, config: Config) -> None:
    """Write the reflections file."""
    config.ensure_memory_dir()
    config.reflections_path.write_text(reflections.rstrip() + "\n")


def _trim_old_observations(config: Config) -> None:
    """Remove observation entries older than retention period."""
    if not config.observations_path.exists():
        return

    content = config.observations_path.read_text()
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.observation_retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Split by date headers (## YYYY-MM-DD)
    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", content, flags=re.MULTILINE)

    kept = []
    for section in sections:
        # Extract date from header
        date_match = re.match(r"## (\d{4}-\d{2}-\d{2})", section.strip())
        if date_match:
            section_date = date_match.group(1)
            if section_date >= cutoff_str:
                kept.append(section)
        else:
            # Keep non-date sections (like the header)
            kept.append(section)

    config.observations_path.write_text("".join(kept).rstrip() + "\n")
