"""Reflector: condense observations into long-term reflections."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .config import Config
from .llm import compress
from pathlib import Path


REFLECTOR_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "reflector.md"


def run_reflector(config: Config | None = None, dry_run: bool = False) -> str | None:
    """Read observations + reflections, condense, write updated reflections.

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
    user_content = (
        f"## Current reflections\n\n{reflections}\n\n"
        f"---\n\n"
        f"## Current observations\n\n{observations}"
    )

    result = compress(system_prompt, user_content, config)

    if dry_run:
        return result

    _write_reflections(result, config)
    _trim_old_observations(config)

    return result


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
