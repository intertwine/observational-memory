"""Reflector: condense observations into long-term reflections."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .llm import compress
from .reflection_metadata import ensure_reflection_metadata, prune_stale_snapshots

_LOGGER = logging.getLogger(__name__)

REFLECTOR_PROMPT_PATH = Path(__file__).parent / "prompts" / "reflector.md"

# Approximate chars-per-token ratio for estimating input size.
_CHARS_PER_TOKEN = 3.5
# Fixed allowance for the fold wrappers ("## Current reflections", separators,
# the "## Observations (chunk i/N)" header, and the intermediate-chunk NOTE),
# plus a safety margin.
_FOLD_WRAPPER_CHARS = 400
# Floor on the observations chunk so a fold always makes forward progress.
_MIN_CHUNK_CHARS = 4000
# Standalone fallback chunk budget for callers without a Config (mirrors the
# OM_REFLECTOR_MAX_INPUT_TOKENS=45000 * OM_REFLECTOR_OBSERVATION_CHUNK_RATIO=0.6
# defaults). The reflector path always passes an explicit, config-derived
# budget; this only backstops direct _chunk_observations callers (e.g. tests).
_DEFAULT_CHUNK_BUDGET_CHARS = int(45_000 * _CHARS_PER_TOKEN * 0.6)
# max_tokens for reflector output (200-600 lines needs room)
_REFLECTOR_MAX_OUTPUT_TOKENS = 8192
# Sent as the per-fold reflections context when the input budget leaves no room
# for any reflections at all (a pathological config). Keeps the fold under the
# per-call ceiling instead of re-sending the full running document.
_REFLECTIONS_BUDGET_EXHAUSTED = "[... reflections context omitted: reflector input budget exhausted ...]"
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

    inputs = _gather_reflection_inputs(config)
    if inputs is None:
        return None

    if inputs.single_pass:
        result = _reflect_single(
            inputs.system_prompt,
            inputs.reflections,
            inputs.observations,
            config,
            inputs.auto_memory,
            inputs.amem_changed,
        )
    else:
        # Too large — chunk observations and fold incrementally
        result = _reflect_chunked(
            inputs.system_prompt,
            inputs.reflections,
            inputs.observations,
            config,
            inputs.auto_memory,
            inputs.amem_changed,
        )

    return finalize_reflection(result, config, inputs.raw_observations, dry_run=dry_run)


@dataclass
class ReflectionInputs:
    """Gathered reflector inputs and the single-pass vs chunked decision."""

    system_prompt: str
    reflections: str
    observations: str  # filtered to new observations since last reflection
    raw_observations: str
    auto_memory: str
    amem_changed: bool
    single_pass: bool


class ChunkingRequired(RuntimeError):
    """Raised by the async path when the input is too large for one Batch request."""


def _gather_reflection_inputs(config: Config) -> ReflectionInputs | None:
    """Read observations/reflections, filter to new work, and size the request.

    Returns None when there's nothing to reflect on. Shared by the synchronous
    reflector and the async (Batch) submit path so both see identical inputs.
    """
    raw_observations = ""
    if config.observations_path.exists():
        raw_observations = config.observations_path.read_text()

    reflections = ""
    if config.reflections_path.exists():
        reflections = config.reflections_path.read_text()

    last_reflected_date = _parse_last_reflected(reflections)
    observations = _filter_new_observations(raw_observations, last_reflected_date) if raw_observations.strip() else ""

    # Auto-memory is included only when it changed since the last reflection
    # (it may be empty when all files were deleted — the reflector still runs to
    # clean up stale facts).
    auto_memory = ""
    amem_changed = _auto_memory_changed_since_reflection(config)
    if not observations.strip():
        if not amem_changed:
            return None
        auto_memory = _gather_auto_memory_context(config)
    elif amem_changed:
        auto_memory = _gather_auto_memory_context(config)

    system_prompt = _load_reflector_prompt()
    total_input_chars = len(system_prompt) + len(reflections) + len(observations) + len(auto_memory)
    single_pass = (total_input_chars / _CHARS_PER_TOKEN) <= config.reflector_max_input_tokens
    return ReflectionInputs(
        system_prompt=system_prompt,
        reflections=reflections,
        observations=observations,
        raw_observations=raw_observations,
        auto_memory=auto_memory,
        amem_changed=amem_changed,
        single_pass=single_pass,
    )


def prepare_single_pass_reflection(config: Config) -> tuple[str, str, int, ReflectionInputs] | None:
    """Build the single-pass reflector request for async (Batch) submission.

    Returns ``(system_prompt, user_content, max_output_tokens, inputs)`` or None
    when there's nothing to reflect on. Raises :class:`ChunkingRequired` when the
    input would need chunking (the caller should fall back to a synchronous run)
    or when cluster mode is active (unsupported for async).
    """
    if _cluster_enabled(config):
        raise ChunkingRequired("cluster-mode reflection is not supported for async Batch")
    inputs = _gather_reflection_inputs(config)
    if inputs is None:
        return None
    if not inputs.single_pass:
        raise ChunkingRequired("reflect input is too large for a single Batch request")
    user_content = _single_pass_user_content(
        inputs.reflections, inputs.observations, config, inputs.auto_memory, inputs.amem_changed
    )
    return inputs.system_prompt, user_content, _REFLECTOR_MAX_OUTPUT_TOKENS, inputs


def finalize_reflection(result: str, config: Config, raw_observations: str, dry_run: bool = False) -> str:
    """Stamp, normalize, and persist a raw reflector output.

    Shared by the synchronous reflector and the async (Batch) apply path so a
    deferred result is processed identically to an immediate one: stamp the
    timestamps, ensure metadata, prune stale snapshots, then (unless dry-run)
    write reflections.md, trim consumed observations, and reindex search.
    """
    # Cap a runaway reflector output before it's stamped and persisted. The cap
    # is provider-agnostic — it runs here, where the sync and async paths
    # converge, so it also covers the openai-chatgpt (Codex) Responses path that
    # can't honor max_output_tokens.
    result = _cap_reflector_output(result, config.reflector_output_max_chars)

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


def _bound_reflections_context(
    reflections: str,
    max_chars: int,
    diagnostics: dict[str, int] | None = None,
) -> str:
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

    ``diagnostics`` carries the chunked path's budget breakdown so the warning
    can report the *configured* reflections cap alongside the *effective*
    per-call cap and the binding ceiling. Without it (single-pass), ``max_chars``
    is itself the configured cap and that is what binds.
    """
    if max_chars <= 0 or len(reflections) <= max_chars:
        return reflections
    marker = "\n\n[... older reflections truncated to fit the reflector input budget ...]\n"
    # For an absurdly small cap (smaller than the marker) just hard-truncate so
    # the result never exceeds max_chars and always carries some real content.
    if max_chars <= len(marker):
        return reflections[:max_chars]
    head = reflections[: max_chars - len(marker)]
    if diagnostics:
        # Report BOTH the configured cap and the effective per-call cap so the
        # operator can tell which ceiling is actually binding (the input-token
        # ceiling can clamp the effective cap below the configured value).
        _LOGGER.warning(
            "reflections.md context (%d chars) exceeds the effective reflector cap; "
            "sending the head only. configured_reflections_cap=%d effective_reflections_cap=%d "
            "max_input_tokens=%d observation_chunk_budget=%d. Raise OM_REFLECTOR_MAX_INPUT_TOKENS "
            "(or OM_REFLECTOR_CONTEXT_MAX_CHARS) or compress reflections to avoid dropping older sections.",
            len(reflections),
            diagnostics["configured_reflections_cap"],
            diagnostics["effective_reflections_cap"],
            diagnostics["max_input_tokens"],
            diagnostics["observation_chunk_budget"],
        )
    else:
        _LOGGER.warning(
            "reflections.md context (%d chars) exceeds OM_REFLECTOR_CONTEXT_MAX_CHARS=%d; "
            "sending the head only. Raise the cap or compress reflections to avoid dropping older sections.",
            len(reflections),
            max_chars,
        )
    return head + marker


def _cap_reflector_output(result: str, max_chars: int) -> str:
    """Cap a runaway reflector *output* at a section boundary.

    Provider-agnostic safety net for the prompt-side length budget: a strong
    reasoning model can blow past the target, and the openai-chatgpt (Codex)
    Responses path can't enforce max_output_tokens, so nothing else bounds the
    emitted document. When the output overruns ``max_chars`` (see
    ``Config.reflector_output_max_chars``), trim back to the last complete
    "## " section heading before the cap — never mid-section, which would leave
    reflections.md with a half-written entry — append a marker, and log a
    warning naming the cap. ``max_chars <= 0`` disables the cap. The default is
    generous, so this only fires on a genuine runaway.
    """
    if max_chars <= 0 or len(result) <= max_chars:
        return result

    marker = "\n\n[... reflections truncated to fit OM_REFLECTOR_OUTPUT_MAX_CHARS ...]\n"
    # Find the last "## " section boundary that starts at or before the cap (after
    # reserving room for the marker) so the trimmed document never exceeds the cap
    # and never ends mid-section.
    budget = max(max_chars - len(marker), 0)
    boundary = result.rfind("\n## ", 0, budget)
    if boundary <= 0:
        # No section boundary fits under the cap (e.g. a giant first section or a
        # cap smaller than the head). Keep the head up to the budget rather than
        # dropping everything — still never exceeding max_chars.
        head = result[:budget]
    else:
        head = result[: boundary + 1]  # keep the trailing newline before the next "## "

    _LOGGER.warning(
        "reflector output (%d chars) exceeds OM_REFLECTOR_OUTPUT_MAX_CHARS=%d; "
        "trimmed at a section boundary. Tighten the reflector prompt or raise the cap.",
        len(result),
        max_chars,
    )
    return head.rstrip() + marker


def _reflect_single(
    system_prompt: str,
    reflections: str,
    observations: str,
    config: Config,
    auto_memory: str = "",
    amem_changed: bool = False,
) -> str:
    """Single-pass reflection for small observation sets."""
    user_content = _single_pass_user_content(reflections, observations, config, auto_memory, amem_changed)
    return compress(system_prompt, user_content, config, max_tokens=_REFLECTOR_MAX_OUTPUT_TOKENS, operation="reflector")


def _single_pass_user_content(
    reflections: str,
    observations: str,
    config: Config,
    auto_memory: str = "",
    amem_changed: bool = False,
) -> str:
    """Build the single-pass reflector user content (shared by sync and async)."""
    amem_section = _auto_memory_section(auto_memory, amem_changed)
    obs_section = f"## Current observations\n\n{observations}" if observations.strip() else "(no new observations)"
    bounded = _bound_reflections_context(reflections, config.reflector_context_max_chars)
    return f"## Current reflections\n\n{bounded}\n\n---\n\n{obs_section}{amem_section}"


def _reflector_budgets(system_prompt: str, amem_section: str, config: Config) -> tuple[int, int]:
    """Split the per-call input budget between reflections context and obs chunk.

    Every fold must satisfy ``system_prompt + reflections + chunk + wrappers <=
    max_input_chars`` (``OM_REFLECTOR_MAX_INPUT_TOKENS`` * ``_CHARS_PER_TOKEN``).
    We reserve the system prompt, auto-memory section, and a fixed wrapper
    allowance, then share what remains: the reflections context is capped by
    ``OM_REFLECTOR_CONTEXT_MAX_CHARS`` but never larger than what leaves a
    minimum chunk for observations, and the chunk gets the rest. A configured
    cap of 0 (disabled) is treated as "as large as the budget allows" — the
    chunked path can never re-send a truly unbounded document and stay under the
    ceiling.

    Returns ``(reflections_cap, chunk_budget)``.

    Observations get ``OM_REFLECTOR_OBSERVATION_CHUNK_RATIO`` (default 0.6) of
    the total budget: larger chunks mean fewer folds, and each fold re-sends the
    reflections context, so maximizing the chunk minimizes the repeated re-send
    cost. The reflections context gets what's left after the system prompt,
    auto-memory, and wrappers — capped by the configured value.
    """
    max_input_chars = int(config.reflector_max_input_tokens * _CHARS_PER_TOKEN)
    chunk_budget = max(int(max_input_chars * config.reflector_observation_chunk_ratio), _MIN_CHUNK_CHARS)
    remainder = max_input_chars - chunk_budget - len(system_prompt) - len(amem_section) - _FOLD_WRAPPER_CHARS
    max_reflections = max(remainder, 0)
    configured_cap = config.reflector_context_max_chars
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
    reflections_cap, chunk_budget = _reflector_budgets(system_prompt, amem_section, config)
    chunks = _chunk_observations(observations, chunk_budget)
    # Surface the configured vs effective reflections cap honestly: the
    # input-token ceiling (or chunk ratio) can clamp the effective cap below the
    # configured OM_REFLECTOR_CONTEXT_MAX_CHARS, and the warning must say which.
    budget_diagnostics = {
        "configured_reflections_cap": config.reflector_context_max_chars,
        "effective_reflections_cap": reflections_cap,
        "max_input_tokens": config.reflector_max_input_tokens,
        "observation_chunk_budget": chunk_budget,
    }

    # A pathological config (too-low OM_REFLECTOR_MAX_INPUT_TOKENS, or an extreme
    # observation chunk ratio) can leave no room for reflections context at all.
    # An effective cap of 0 must mean "send a marker only" here — NOT "unbounded",
    # which is how _bound_reflections_context reads max_chars<=0 — otherwise the
    # full running document is re-sent and the per-call ceiling this budget exists
    # to enforce is violated. Warn once so the operator can fix the config.
    budget_exhausted = reflections_cap <= 0
    if budget_exhausted:
        _LOGGER.warning(
            "reflector input budget leaves no room for reflections context "
            "(configured_reflections_cap=%d effective_reflections_cap=0 "
            "max_input_tokens=%d observation_chunk_budget=%d); folding with a marker "
            "only. Raise OM_REFLECTOR_MAX_INPUT_TOKENS or lower "
            "OM_REFLECTOR_OBSERVATION_CHUNK_RATIO.",
            config.reflector_context_max_chars,
            config.reflector_max_input_tokens,
            chunk_budget,
        )

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
        # per-fold context is capped. A 0 cap means no room at all -> marker only
        # (never the full document, which _bound_reflections_context would re-send
        # for max_chars<=0).
        if budget_exhausted:
            bounded = _REFLECTIONS_BUDGET_EXHAUSTED
        else:
            bounded = _bound_reflections_context(running_reflections, reflections_cap, budget_diagnostics)
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


def _split_to_width(text: str, max_len: int) -> list[str]:
    """Split text into pieces no longer than ``max_len``, preferring line breaks.

    A line longer than ``max_len`` is hard-split. Guarantees every piece is
    ``<= max_len`` so a single oversized date section can't blow the budget.
    """
    if max_len <= 0 or len(text) <= max_len:
        return [text]
    pieces: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if cur:
                pieces.append(cur)
                cur = ""
            for j in range(0, len(line), max_len):
                pieces.append(line[j : j + max_len])
            continue
        if cur and len(cur) + len(line) > max_len:
            pieces.append(cur)
            cur = ""
        cur += line
    if cur:
        pieces.append(cur)
    return pieces


def _chunk_observations(observations: str, budget_chars: int | None = None) -> list[str]:
    """Split observations into chunks that each fit within ``budget_chars``.

    Splits on ``## YYYY-MM-DD`` date headers and packs whole days into chunks,
    re-prepending the ``# Observations`` header to each chunk for context.
    ``budget_chars`` is the room left for observations after the reflections
    context and system prompt are reserved (see ``_reflector_budgets``); when
    omitted it falls back to a conservative standalone default.

    A single date section larger than the budget is split within the day (on line
    boundaries, hard-splitting if needed) so every emitted chunk — header
    included — stays ``<= budget_chars``. We never emit a header-only chunk.
    """
    if budget_chars is None:
        budget_chars = _DEFAULT_CHUNK_BUDGET_CHARS

    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", observations, flags=re.MULTILINE)

    header = ""
    date_sections = []
    for section in sections:
        if re.match(r"## \d{4}-\d{2}-\d{2}", section.strip()):
            date_sections.append(section)
        else:
            header = section

    if not date_sections:
        # No date structure — return whole if it fits, else hard-split to width.
        return [observations] if len(observations) <= budget_chars else _split_to_width(observations, budget_chars)

    # Every chunk re-carries the header, so content must fit in the remainder.
    max_content = max(budget_chars - len(header), 1)
    pieces: list[str] = []
    for section in date_sections:
        pieces.extend(_split_to_width(section, max_content))

    chunks: list[str] = []
    current = ""  # content only; header is added at flush
    for piece in pieces:
        if current and len(header) + len(current) + len(piece) > budget_chars:
            chunks.append(header + current)
            current = ""
        current += piece
    if current:
        chunks.append(header + current)

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
    if estimated_tokens <= config.reflector_max_input_tokens:
        result = _reflect_single(system_prompt, reflections, raw_observations, config, auto_memory, amem_changed)
    else:
        result = _reflect_chunked(system_prompt, reflections, raw_observations, config, auto_memory, amem_changed)

    # Same provider-agnostic output cap as finalize_reflection (the cluster
    # reflector persists through its own path, so apply it here too).
    result = _cap_reflector_output(result, config.reflector_output_max_chars)

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
