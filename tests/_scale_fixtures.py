"""Deterministic, structure-faithful fixtures for reflector scale tests (#71, M2).

The reflector-scaling problem is *algorithmic*: chunked reflection re-sends a
growing ``reflections.md`` on every fold, which is O(chunks x reflections-size).
At 10x/100x memory that stops being tractable regardless of caps.

To exercise that behavior in fast, deterministic CI we do NOT allocate
multi-megabyte strings. Instead we hold the **shape** fixed (the real OM
reflections layout: Core Identity, Active Projects with per-project H3
subsections, Preferences & Opinions, Relationship & Communication, Key Facts &
Context, Recent Themes, Archive; plus dated observation sections) and express
2x / 10x / 100x as the *ratio of reflections size to the per-call reflector
budget*. The tests pair a given shape with a SHRUNKEN ``reflector_max_input_tokens``
so the same O(chunks x size) pressure is visible at small byte counts.

A scenario therefore carries both:
  - a fixture size (chars), and
  - a recommended shrunken input-token budget,
chosen so ``reflections_chars / per_call_budget_chars`` lands near the scenario's
intended multiple of the v0.6.7 target document (``OM_REFLECTOR_CONTEXT_MAX_CHARS``
default 48000).

No private or real data: all text is synthetic and generated from fixed seeds.
"""

from __future__ import annotations

from dataclasses import dataclass

# The v0.6.7 "target" reflections.md size (configured cap default). 1x == this.
TARGET_REFLECTIONS_CHARS = 48_000


def _filler(label: str, n_lines: int, width: int = 72) -> str:
    """Deterministic synthetic body lines (no randomness, no real data)."""
    lines = []
    for i in range(n_lines):
        # Pad/truncate to a stable width so size math is predictable.
        base = f"- {label} note {i}: durable synthetic fact for scale testing"
        if len(base) < width:
            base = base + " " + "x" * (width - len(base) - 1)
        lines.append(base[:width])
    return "\n".join(lines)


def _project_subsection(name: str, n_lines: int) -> str:
    return f"### {name}\n\n" + _filler(f"{name}", n_lines) + "\n"


def make_reflections(target_chars: int, *, n_projects: int = 6) -> str:
    """Synthesize a reflections.md with the real OM section structure.

    Grows to roughly ``target_chars`` by padding section bodies. The head
    (title, timestamps, Core Identity, Active Projects) always comes first, as
    in the real format, so head-only truncation is observable as "keeps the top
    sections, drops Archive/Recent Themes".
    """
    prelude = "# Reflections\n\n*Last updated: 2026-05-01 09:00 UTC*\n*Last reflected: 2026-05-01*\n\n"

    # Fixed-size durable head sections.
    head = "## Core Identity\n\n" + _filler("core-identity", 8) + "\n\n## Active Projects\n\n"
    for p in range(n_projects):
        head += "\n" + _project_subsection(f"project-{p}", 6) + "\n"

    tail_sections = [
        ("Preferences & Opinions", "preferences"),
        ("Relationship & Communication", "relationship"),
        ("Key Facts & Context", "key-facts"),
        ("Recent Themes", "recent-themes"),
        ("Archive", "archive"),
    ]

    fixed = prelude + head
    tail_headers = "".join(f"\n## {title}\n\n" for title, _ in tail_sections)

    # Distribute the remaining budget across the tail sections (Archive last so
    # head-only truncation visibly drops it).
    remaining = max(target_chars - len(fixed) - len(tail_headers), 0)
    per_section_chars = remaining // max(len(tail_sections), 1)
    # ~73 chars per filler line including newline.
    per_section_lines = max(per_section_chars // 73, 1)

    body = prelude + head
    for title, label in tail_sections:
        body += f"\n## {title}\n\n" + _filler(label, per_section_lines) + "\n"
    return body


def make_observations(target_chars: int, *, start_day: int = 2) -> str:
    """Synthesize an observations.md of dated ``## YYYY-MM-DD`` sections.

    Spreads ``target_chars`` across consecutive May-2026 day sections so the
    chunker (which splits on date headers) has realistic multi-day structure to
    pack into folds.
    """
    header = "# Observations\n\n"
    # ~73 chars/line; aim for ~20 days, sized to hit target.
    n_days = 20
    per_day_chars = max((target_chars - len(header)) // n_days, 73)
    per_day_lines = max(per_day_chars // 73, 1)

    body = header
    day = start_day
    for _ in range(n_days):
        # Roll into next month if needed (kept simple/deterministic).
        date = f"2026-05-{day:02d}" if day <= 28 else f"2026-06-{day - 28:02d}"
        body += f"## {date}\n\n" + _filler(f"obs-{date}", per_day_lines) + "\n\n"
        day += 1
    return body


@dataclass(frozen=True)
class ScaleScenario:
    """A scale shape plus the shrunken budget that surfaces it deterministically.

    ``multiple`` is the intended growth over the v0.6.7 target document (2x/10x/
    100x). ``reflections_chars`` is the synthetic fixture size. ``input_tokens``
    is the SHRUNKEN ``OM_REFLECTOR_MAX_INPUT_TOKENS`` paired with it so the ratio
    ``reflections_chars / per_call_budget_chars`` reproduces the real-corpus
    pressure at that multiple without allocating real-corpus byte counts.
    """

    name: str
    multiple: int
    # The *real-corpus* sizes this shape stands in for (documentation only — we
    # do NOT allocate these). 2x of the v0.6.7 target reflections.md is ~96k;
    # 10x ~480k; 100x ~4.8M.
    real_reflections_chars: int
    real_observations_chars: int
    # The SHRUNKEN fixture we actually generate (small -> fast CI).
    fixture_reflections_chars: int
    fixture_observations_chars: int
    # The SHRUNKEN per-call ceiling paired with the fixture. Chosen so
    # fixture_reflections_chars / per_call_budget_chars(input_tokens) reproduces
    # the scenario's intended multiple of how-much-a-fold-can-hold. Smaller
    # input_tokens at the same fixture size == larger multiple.
    input_tokens: int


# Shrunken-but-faithful scenarios. Byte sizes are small (fast CI) but the RATIO
# of generated reflections size to the derived per-call budget reproduces
# 2x/10x/100x pressure. We hold the fixture size roughly constant across the
# large scenarios and SHRINK the budget to climb the multiple — this keeps
# allocation tiny while making the size-to-budget ratio honestly distinct
# (10x ratio < 100x ratio). See module docstring and ``per_call_budget_chars``.
#
# 2x: reflections ~2x target, generous budget (the v0.6.7 knobs handle it,
#     configured cap binds, most reflections context survives).
# 10x: reflections far exceed a single fold's budget -> full-document resend
#      drops the majority of context every fold.
# 100x: budget holds only a sliver of the document -> full-document resend is
#       outright intractable; only section-targeted reflection keeps per-fold
#       size proportional to touched sections, not whole-document size.
SCENARIOS: dict[str, ScaleScenario] = {
    "2x": ScaleScenario(
        name="2x",
        multiple=2,
        real_reflections_chars=2 * TARGET_REFLECTIONS_CHARS,  # ~96k
        real_observations_chars=1_500_000,
        fixture_reflections_chars=2 * TARGET_REFLECTIONS_CHARS,  # ~96k (cheap)
        fixture_observations_chars=120_000,
        input_tokens=45_000,  # v0.6.7 default; configured 48k cap binds
    ),
    "10x": ScaleScenario(
        name="10x",
        multiple=10,
        real_reflections_chars=10 * TARGET_REFLECTIONS_CHARS,  # ~480k
        real_observations_chars=7_500_000,
        # Generate a modest fixture; shrink the budget instead of allocating 480k.
        fixture_reflections_chars=120_000,
        fixture_observations_chars=120_000,
        input_tokens=12_000,
    ),
    "100x": ScaleScenario(
        name="100x",
        multiple=100,
        real_reflections_chars=100 * TARGET_REFLECTIONS_CHARS,  # ~4.8M
        real_observations_chars=75_000_000,
        # We grow the FIXTURE (cheap string building, sub-millisecond) and keep
        # the budget above the production floors (_MIN_CHUNK_CHARS and a
        # realistic system-prompt) so the per-call ceiling genuinely holds. The
        # large fixture against the same 10x budget yields a far higher
        # size-to-budget ratio, i.e. 100x pressure — still no megabyte alloc on
        # the order of the real 4.8M corpus.
        fixture_reflections_chars=600_000,
        fixture_observations_chars=120_000,
        input_tokens=12_000,
    ),
}


# Mirror of reflect._CHARS_PER_TOKEN; kept local so the fixtures stay importable
# without pulling production internals into fixture math. Asserted equal in the
# tests so a production change can't silently desync this.
CHARS_PER_TOKEN = 3.5


def per_call_budget_chars(input_tokens: int) -> int:
    """The hard per-call char ceiling for a given OM_REFLECTOR_MAX_INPUT_TOKENS."""
    return int(input_tokens * CHARS_PER_TOKEN)


def size_to_budget_ratio(scenario: ScaleScenario) -> float:
    """How many full-document resends would overflow one fold's budget.

    This is the scale knob: ratio ~1 means a fold can hold the document; ratio
    >> 1 means full-document resend cannot fit and only a section-targeted
    strategy keeps per-fold size bounded.
    """
    return scenario.fixture_reflections_chars / per_call_budget_chars(scenario.input_tokens)


def make_scenario(scenario: ScaleScenario) -> tuple[str, str]:
    """Build (reflections, observations) for a scenario.

    We never literally allocate the real-corpus byte counts. We generate the
    small ``fixture_*`` sizes and rely on the shrunken ``input_tokens`` so the
    size-to-budget ratio reproduces the scenario's multiple. Tests assert
    against that ratio and against per-fold prompt sizes, not raw byte counts.
    """
    return (
        make_reflections(scenario.fixture_reflections_chars),
        make_observations(scenario.fixture_observations_chars),
    )
