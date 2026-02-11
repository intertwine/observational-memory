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


def run_reflector(config: Config | None = None, dry_run: bool = False) -> str | None:
    """Read observations + reflections, condense, write updated reflections.

    When observations are small enough, processes in a single LLM call.
    When observations are large, chunks them by date section and folds
    each chunk into the reflections incrementally.

    Args:
        config: Runtime config.
        dry_run: If True, return result without writing.

    Returns:
        The new reflections text, or None if nothing to reflect on.
    """
    if config is None:
        config = Config()

    observations = ""
    if config.observations_path.exists():
        observations = config.observations_path.read_text()

    if not observations.strip():
        return None

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

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
