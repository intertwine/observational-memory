"""Reflector: condense observations into long-term reflections."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .llm import compress
from .reflection_metadata import ensure_reflection_metadata, prune_stale_snapshots

_LOGGER = logging.getLogger(__name__)

REFLECTOR_PROMPT_PATH = Path(__file__).parent / "prompts" / "reflector.md"

# Approximate chars-per-token ratio for estimating input size.
_CHARS_PER_TOKEN = 3.5
# Maximum input tokens to send in a single reflector call.
# Kept conservative to avoid HTTP body size limits on some networks.
_MAX_INPUT_TOKENS = 12_000
# Total per-call input budget in chars (~_MAX_INPUT_TOKENS tokens). Every
# reflector call (system prompt + reflections context + observations chunk +
# wrappers) must stay under this ceiling.
_MAX_INPUT_CHARS = int(_MAX_INPUT_TOKENS * _CHARS_PER_TOKEN)
# Fixed allowance for the fold wrappers ("## Current reflections", separators,
# the "## Observations (chunk i/N)" header, and the intermediate-chunk NOTE),
# plus a safety margin.
_FOLD_WRAPPER_CHARS = 400
# Floor on the observations chunk so a fold always makes forward progress.
_MIN_CHUNK_CHARS = 4000
# max_tokens for reflector output (200-600 lines needs room)
_REFLECTOR_MAX_OUTPUT_TOKENS = 8192
_MAX_COMPETING_SNAPSHOT_CHARS = 4000

# Regex for the "Last reflected" timestamp line in reflections.md
_LAST_REFLECTED_RE = re.compile(r"^\*Last reflected:\s*(\d{4}-\d{2}-\d{2})\b.*\*$", re.MULTILINE)
# Regex for the "Last updated" timestamp line
_LAST_UPDATED_RE = re.compile(r"^\*Last updated:.*\*$", re.MULTILINE)
_LAST_UPDATED_VALUE_RE = re.compile(r"^\*Last updated:\s*(.+?)\s*\*$", re.MULTILINE)


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

    if _cluster_enabled(config):
        return _run_cluster_reflector(config, dry_run=dry_run)

    raw_observations = ""
    if config.observations_path.exists():
        raw_observations = config.observations_path.read_text()

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

    # Filter to only new observations since last reflection
    last_reflected_date = _parse_last_reflected(reflections)
    observations = _filter_new_observations(raw_observations, last_reflected_date) if raw_observations.strip() else ""

    # Check for auto-memory context, but only invoke the reflector for it
    # if auto-memory has actually changed since the last reflection.
    # Note: auto-memory may be empty string (all files deleted) — the reflector
    # still needs to run to clean up stale facts from reflections.
    auto_memory = ""
    amem_changed = _auto_memory_changed_since_reflection(config)
    if not observations.strip():
        # No new observations — only proceed if auto-memory changed
        if not amem_changed:
            return None
        auto_memory = _gather_auto_memory_context(config)
    else:
        # New observations exist — include auto-memory as supplementary context
        if amem_changed:
            auto_memory = _gather_auto_memory_context(config)

    system_prompt = _load_reflector_prompt()

    # Estimate total input size
    total_input_chars = len(system_prompt) + len(reflections) + len(observations) + len(auto_memory)
    estimated_tokens = total_input_chars / _CHARS_PER_TOKEN

    if estimated_tokens <= _MAX_INPUT_TOKENS:
        # Small enough — single pass
        result = _reflect_single(system_prompt, reflections, observations, config, auto_memory, amem_changed)
    else:
        # Too large — chunk observations and fold incrementally
        result = _reflect_chunked(system_prompt, reflections, observations, config, auto_memory, amem_changed)

    # Programmatically stamp the "Last reflected" timestamp so we don't
    # rely on the LLM to format it correctly.
    latest_obs_date = _extract_latest_observation_date(raw_observations)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    result = _stamp_timestamps(result, now_utc, latest_obs_date or now_utc)
    result = ensure_reflection_metadata(result, node="local")
    result, _summary = prune_stale_snapshots(
        result,
        ttl_days=config.snapshot_ttl_days,
        action=config.snapshot_expiry_action,
    )

    if dry_run:
        return result

    _write_reflections(result, config)
    _trim_old_observations(config)
    _reindex_if_enabled(config)

    return result


def reflector_catchup_needed(config: Config | None = None, now_utc: datetime | None = None) -> bool:
    """Return True when reflections lag behind the newest observation date.

    This lets normal observer runs repair missed daily reflection windows,
    such as when a laptop is asleep during the scheduled background run.
    """
    if config is None:
        config = Config()

    if not config.observations_path.exists():
        return False

    observations = config.observations_path.read_text()
    latest_obs_date = _extract_latest_observation_date(observations)
    if latest_obs_date is None:
        return False

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

    last_reflected_date = _parse_last_reflected(reflections)
    if last_reflected_date is None:
        return True

    if latest_obs_date <= last_reflected_date:
        return False

    last_updated_at = _parse_last_updated(reflections)
    if last_updated_at is None:
        return True

    # Catch up only after the daily reflection window is actually overdue.
    # This avoids duplicate LLM calls on normal days when observations roll
    # into the next UTC date before the local 04:00 background run has fired.
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    return now_utc - last_updated_at > timedelta(hours=24)


def _auto_memory_changed_since_reflection(config: Config) -> bool:
    """Return True if auto-memory was scanned more recently than the last reflection.

    Compares the cursor's ``claude-memory.last_scan`` against the reflections'
    ``Last updated`` timestamp.
    """
    cursor = config.load_cursor()
    amem_cursor = cursor.get("claude-memory", {})

    last_scan_str = amem_cursor.get("last_scan")
    if not last_scan_str:
        return False  # auto-memory was never scanned

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

    last_updated = _parse_last_updated(reflections)
    if last_updated is not None:
        try:
            last_scan_dt = datetime.fromisoformat(last_scan_str)
            if last_scan_dt.tzinfo is None:
                last_scan_dt = last_scan_dt.replace(tzinfo=timezone.utc)
            if last_scan_dt <= last_updated:
                return False  # auto-memory hasn't changed since last reflection
        except (ValueError, TypeError):
            pass  # parse error — assume changed

    return True


def _gather_auto_memory_context(config: Config) -> str:
    """Collect auto-memory content as supplementary reflector input.

    Reads all Claude Code auto-memory files and formats them as a
    cross-project facts section. Returns empty string if no auto-memory
    files exist.
    """
    from .transcripts.auto_memory import find_memory_directories, parse_memory_file, scan_memory_files

    dirs = find_memory_directories(config.claude_projects_dir)
    if not dirs:
        return ""

    sections: list[str] = []
    for memory_dir in dirs:
        files = scan_memory_files(memory_dir)
        if not files:
            continue

        project_slug = files[0].project_slug
        items: list[str] = []
        for mf in files:
            doc = parse_memory_file(mf)
            # Truncate long entries to keep reflector context manageable
            body = doc.content[:500].strip()
            if len(doc.content) > 500:
                body += " [...]"
            label = doc.metadata.get("name", mf.path.stem)
            items.append(f"- **{label}**: {body}")

        if items:
            sections.append(f"### Project: {project_slug}\n" + "\n".join(items))

    if not sections:
        return ""

    return "## Auto-Memory (cross-project facts)\n\n" + "\n\n".join(sections)


def _auto_memory_section(auto_memory: str, amem_changed: bool) -> str:
    """Build the optional auto-memory section for reflector input."""
    if auto_memory:
        return f"\n\n---\n\n{auto_memory}"
    if amem_changed:
        # Auto-memory files were deleted — tell the reflector to clean up stale facts.
        return (
            "\n\n---\n\n## Auto-Memory (cross-project facts)\n\n"
            "(All auto-memory files have been removed. Remove any facts in reflections "
            "that were sourced exclusively from auto-memory and are not corroborated by observations.)"
        )
    return ""


def _bound_reflections_context(reflections: str, max_chars: int) -> str:
    """Cap the reflections.md context fed back to the reflector.

    This bounds *input* size only — the reflector still emits a complete
    document, so a single bounded pass doesn't shrink stored memory. In the
    chunked path it also stops the running document from being re-sent in full
    on every fold (the O(chunks x size) cost the issue calls out).

    The default cap (see ``Config.reflector_context_max_chars``) is generous, so
    this only trims pathologically large documents. When it does, it keeps the
    head — durable identity/projects sit at the top of the reflections format —
    appends a marker, and logs a warning so the operator can raise the cap or
    compress the document. ``max_chars <= 0`` disables the bound.
    """
    if max_chars <= 0 or len(reflections) <= max_chars:
        return reflections
    marker = "\n\n[... older reflections truncated to fit OM_REFLECTOR_CONTEXT_MAX_CHARS ...]\n"
    # For an absurdly small cap (smaller than the marker) just hard-truncate so
    # the result never exceeds max_chars and always carries some real content.
    if max_chars <= len(marker):
        return reflections[:max_chars]
    head = reflections[: max_chars - len(marker)]
    _LOGGER.warning(
        "reflections.md context (%d chars) exceeds OM_REFLECTOR_CONTEXT_MAX_CHARS=%d; "
        "sending the head only. Raise the cap or compress reflections to avoid dropping older sections.",
        len(reflections),
        max_chars,
    )
    return head + marker


def _reflect_single(
    system_prompt: str,
    reflections: str,
    observations: str,
    config: Config,
    auto_memory: str = "",
    amem_changed: bool = False,
) -> str:
    """Single-pass reflection for small observation sets."""
    amem_section = _auto_memory_section(auto_memory, amem_changed)
    obs_section = f"## Current observations\n\n{observations}" if observations.strip() else "(no new observations)"
    bounded = _bound_reflections_context(reflections, config.reflector_context_max_chars)
    user_content = f"## Current reflections\n\n{bounded}\n\n---\n\n{obs_section}{amem_section}"
    return compress(system_prompt, user_content, config, max_tokens=_REFLECTOR_MAX_OUTPUT_TOKENS, operation="reflector")


def _reflector_budgets(system_prompt: str, amem_section: str, configured_cap: int) -> tuple[int, int]:
    """Split the per-call input budget between reflections context and obs chunk.

    Every fold must satisfy ``system_prompt + reflections + chunk + wrappers <=
    _MAX_INPUT_CHARS``. We reserve the system prompt, auto-memory section, and a
    fixed wrapper allowance, then share what remains: the reflections context is
    capped by ``OM_REFLECTOR_CONTEXT_MAX_CHARS`` but never larger than what leaves
    a minimum chunk for observations, and the chunk gets the rest. ``configured_cap``
    of 0 (disabled) is treated as "as large as the budget allows" — the chunked
    path can never re-send a truly unbounded document and stay under the ceiling.

    Returns ``(reflections_cap, chunk_budget)``.

    Observations get a fixed ~60% of the total budget: larger chunks mean fewer
    folds, and each fold re-sends the reflections context, so maximizing the
    chunk minimizes the repeated re-send cost. The reflections context gets
    what's left after the system prompt, auto-memory, and wrappers — capped by
    the configured value. A configured cap of 0 (disabled) takes the whole
    remainder; the chunked path can never re-send a truly unbounded document and
    stay under the ceiling.
    """
    chunk_budget = max(int(_MAX_INPUT_CHARS * 0.6), _MIN_CHUNK_CHARS)
    remainder = _MAX_INPUT_CHARS - chunk_budget - len(system_prompt) - len(amem_section) - _FOLD_WRAPPER_CHARS
    max_reflections = max(remainder, 0)
    if configured_cap <= 0:
        reflections_cap = max_reflections
    else:
        reflections_cap = min(configured_cap, max_reflections)
    return reflections_cap, chunk_budget


def _reflect_chunked(
    system_prompt: str,
    reflections: str,
    observations: str,
    config: Config,
    auto_memory: str = "",
    amem_changed: bool = False,
) -> str:
    """Chunked reflection: split observations into date sections, fold each into reflections."""
    # Reserve the auto-memory section for every fold (it actually rides only on
    # the last) so the conservative budget keeps even that fold under the ceiling.
    amem_section = _auto_memory_section(auto_memory, amem_changed)
    reflections_cap, chunk_budget = _reflector_budgets(system_prompt, amem_section, config.reflector_context_max_chars)
    chunks = _chunk_observations(observations, chunk_budget)

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

        # Include auto-memory context only in the final chunk.
        fold_amem = amem_section if is_last else ""

        # Bound the *re-sent* running document so the chunked fold isn't
        # O(chunks x reflections_size) and stays under the per-call budget.
        # running_reflections itself keeps the full latest output; only the
        # per-fold context is capped.
        bounded = _bound_reflections_context(running_reflections, reflections_cap)
        user_content = (
            f"## Current reflections\n\n{bounded}\n\n"
            f"---\n\n"
            f"## Observations (chunk {i}/{len(chunks)})\n\n{chunk}{fold_amem}"
        )
        running_reflections = compress(
            fold_prompt,
            user_content,
            config,
            max_tokens=_REFLECTOR_MAX_OUTPUT_TOKENS,
            operation="reflector",
        )

    return running_reflections


def _chunk_observations(observations: str, budget_chars: int | None = None) -> list[str]:
    """Split observations by date headers into chunks that fit within token limits.

    Groups consecutive date sections until adding another would exceed the
    per-chunk budget. ``budget_chars`` is the room left for observations after
    the reflections context and system prompt are reserved (see
    ``_reflector_budgets``); when omitted it falls back to a conservative
    standalone default. A single date section larger than the budget is kept
    whole (we never split within a day) and may exceed it — a rare edge case.
    """
    if budget_chars is None:
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


def _parse_last_updated(reflections: str) -> datetime | None:
    """Extract the ``Last updated`` timestamp from reflections.md."""
    m = _LAST_UPDATED_VALUE_RE.search(reflections)
    if not m:
        return None

    raw_value = m.group(1).strip()
    if raw_value.lower() == "never":
        return None

    try:
        return datetime.strptime(raw_value, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
            reflections = _LAST_UPDATED_RE.sub(f"{updated_line}\n{reflected_line}", reflections, count=1)
        else:
            # No timestamp lines at all — insert after the title
            title_match = re.match(r"(#[^\n]*\n)", reflections)
            if title_match:
                insert_pos = title_match.end()
                reflections = (
                    reflections[:insert_pos] + f"\n{updated_line}\n{reflected_line}\n" + reflections[insert_pos:]
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
    from .startup_memory import refresh_startup_memory

    config.ensure_memory_dir()
    config.reflections_path.write_text(reflections.rstrip() + "\n")
    refresh_startup_memory(config)


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


def _cluster_enabled(config: Config) -> bool:
    try:
        from .sync import cluster_feature_enabled

        return cluster_feature_enabled(config)
    except Exception:
        return False


def _run_cluster_reflector(config: Config, dry_run: bool = False) -> str | None:
    from .sync.engine import sync_cluster
    from .sync.frontier import frontier_covers, frontier_from_records, frontier_join
    from .sync.materialize import choose_reflection_snapshot, materialize_cluster_memory
    from .sync.store import ClusterStore

    store = ClusterStore.from_config(config)
    materialize_cluster_memory(config, store, reindex=False)

    observations = store.list_records(kind="observation")
    observation_frontier = frontier_from_records(observations)
    selected_snapshot, _catchup_needed = choose_reflection_snapshot(store)
    selected_frontier = {}
    reflections = ""
    if selected_snapshot is not None:
        selected_payload = store.read_payload(selected_snapshot)
        selected_frontier = selected_payload.get("frontier", {})
        reflections = str(selected_payload.get("body") or "")

    amem_changed = _auto_memory_changed_since_reflection(config)
    if frontier_covers(selected_frontier, observation_frontier) and not amem_changed:
        return None

    raw_observations = config.observations_path.read_text() if config.observations_path.exists() else ""
    auto_memory = _gather_auto_memory_context(config) if amem_changed else ""
    system_prompt = _load_reflector_prompt()
    competing = _competing_snapshot_context(store, selected_snapshot, selected_frontier)
    if competing:
        system_prompt += (
            "\n\nYou are merging durable memory snapshots from multiple machines. Preserve durable facts, "
            "reconcile duplicates, and prefer newer explicit corrections. Do not include source-machine chatter "
            "unless it is itself useful memory."
        )
        reflections = (reflections + "\n\n" + competing).strip()

    total_input_chars = len(system_prompt) + len(reflections) + len(raw_observations) + len(auto_memory)
    estimated_tokens = total_input_chars / _CHARS_PER_TOKEN
    if estimated_tokens <= _MAX_INPUT_TOKENS:
        result = _reflect_single(system_prompt, reflections, raw_observations, config, auto_memory, amem_changed)
    else:
        result = _reflect_chunked(system_prompt, reflections, raw_observations, config, auto_memory, amem_changed)

    latest_obs_date = _extract_latest_observation_date(raw_observations)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    result = _stamp_timestamps(result, now_utc, latest_obs_date or now_utc)
    result = ensure_reflection_metadata(result, node=store.cluster_config.node_id)
    result, _summary = prune_stale_snapshots(
        result,
        ttl_days=config.snapshot_ttl_days,
        action=config.snapshot_expiry_action,
    )

    if dry_run:
        return result

    base_snapshot_ids = [selected_snapshot.record_id] if selected_snapshot else []
    frontier = frontier_join(observation_frontier, selected_frontier)
    from .reflection_metadata import filter_reflection_entries_for_cluster

    store.append_record(
        kind="reflection_snapshot",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "reflector", "host_alias": store.cluster_config.node_alias},
        payload={
            "format": "markdown",
            "body": filter_reflection_entries_for_cluster(result),
            "frontier": frontier,
            "input_record_ids": [record.record_id for record in observations],
            "base_snapshot_ids": base_snapshot_ids,
        },
    )
    materialize_cluster_memory(config, store)
    if store.cluster_config.sync_on_reflect:
        try:
            sync_cluster(config, deadline_ms=1500)
        except Exception:
            pass
    return result


def _competing_snapshot_context(store, selected_snapshot, selected_frontier: dict) -> str:
    from .sync.frontier import frontier_compare

    sections = []
    for record in store.list_records(kind="reflection_snapshot"):
        if selected_snapshot is not None and record.record_id == selected_snapshot.record_id:
            continue
        payload = store.read_payload(record)
        frontier = payload.get("frontier", {})
        if frontier_compare(selected_frontier, frontier) == "incomparable":
            body = str(payload.get("body", ""))
            if len(body) > _MAX_COMPETING_SNAPSHOT_CHARS:
                body = body[:_MAX_COMPETING_SNAPSHOT_CHARS].rstrip() + "\n\n[truncated]"
            sections.append(f"## Candidate snapshot {record.record_id}\n\n{body}")
    if not sections:
        return ""
    return "## Competing Cluster Reflection Snapshots\n\n" + "\n\n".join(sections)
