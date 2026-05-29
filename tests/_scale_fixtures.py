"""Deterministic, structure-faithful fixtures for reflector scale tests (#71, M2).

The reflector-scaling problem is *algorithmic*: chunked reflection re-sends a
growing ``reflections.md`` on every fold, which is O(chunks x reflections-size).
At 10x/100x memory that stops being tractable regardless of caps. The cost has
TWO inputs — the number of folds (chunks, driven by observations size) AND the
re-sent reflections prefix size — so a faithful scale fixture must grow BOTH
across the tiers, not just the reflections document.

To exercise that behavior in fast, deterministic CI we hold the **shape** fixed
(the real OM reflections layout) and scale two independent axes per tier:

  1. observations size (and, via the paired shrunken ``input_tokens``, the
     per-fold chunk budget) — so the FOLD COUNT genuinely grows 2x -> 10x ->
     100x. This is the ``O(chunks)`` axis the earlier single-fixture design
     never moved.
  2. reflections size — so the re-sent prefix is realistically large.

The product ``folds x per-fold-resend`` is the real ``O(chunks x size)`` cost
the milestone exists to bound, and it grows monotonically across the tiers
(see ``total_resend_chars`` in the tests).

Structural fidelity matters too. Real OM ``reflections.md`` is dominated by
Active Projects and Archive; the durable *core bundle* (Core Identity,
Preferences & Opinions, Relationship & Communication, Key Facts & Context) is
modest — a few KB total. ``make_reflections`` therefore keeps the core sections
SMALL and fixed and grows the bulk in Active Projects + Archive. That makes the
M3 contract self-consistent: a section-targeted reflector can re-send the whole
(small) core bundle plus one touched section and still stay under a realistic
per-fold ceiling, instead of the earlier design where each core section was
~119k and "keep the core bundle" contradicted "keep per-fold resend small".

No private or real data: all text is synthetic and generated from fixed seeds.
"""

from __future__ import annotations

from dataclasses import dataclass

# The v0.6.7 "target" reflections.md size (configured cap default). 1x == this.
TARGET_REFLECTIONS_CHARS = 48_000

# The durable core bundle is modest in a real reflections.md. We pin it to a
# small, fixed size across ALL tiers so a section-targeted reflector (M3) can
# carry it in every fold under a realistic ceiling. ~73 chars per filler line.
_CORE_SECTION_LINES = 12  # ~900 chars/section -> ~3.6k for the four core sections


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


# A realistic individual section (one project entry, one archived item) is a few
# KB, not hundreds of KB. We hold per-section size roughly constant and grow the
# COUNT of sections as the document grows — that is how a real reflections.md
# scales (more projects + more archived items, each modest). This keeps a
# section-targeted M3 fold (core bundle + one touched section) genuinely compact
# even at 100x, instead of one 160k "project" that no targeting could shrink.
_PER_SECTION_CHARS = 4_000


def make_reflections(target_chars: int, *, n_projects: int | None = None) -> str:
    """Synthesize a reflections.md with the real OM section structure.

    Faithful size distribution: the durable CORE sections (Core Identity,
    Preferences & Opinions, Relationship & Communication, Key Facts & Context)
    are SMALL and fixed-size (~a few KB total) regardless of ``target_chars``,
    as in a real reflections.md. The bulk grows in **Active Projects** and
    **Archive** — the sections that actually dominate a real corpus. So:

      - a section-targeted reflector can re-send the entire (small) core bundle
        plus one touched project and still stay compact; and
      - head-only truncation past the core bundle is observable as "keeps the
        durable core, then truncates mid Active Projects, dropping Recent Themes
        and the heavy Archive".

    Section order matches the real format and keeps durability at the top: Core
    Identity, then the small core-bundle sections (Preferences & Opinions,
    Relationship & Communication, Key Facts & Context), then the heavy Active
    Projects, Recent Themes, and Archive (heavy, last — so head-only truncation
    visibly drops it). The core bundle therefore survives a modest 2x clamp but
    the heavy tail does not, and at 10x/100x even the head clamp falls inside
    Active Projects, dropping later core-tail sections — the legacy failure the
    M3 contract tests document.
    """
    prelude = "# Reflections\n\n*Last updated: 2026-05-01 09:00 UTC*\n*Last reflected: 2026-05-01*\n\n"

    # Fixed, modest core sections (NOT scaled with target_chars). Core Identity
    # first, then the rest of the durable core bundle, all near the top.
    core_identity = "## Core Identity\n\n" + _filler("core-identity", _CORE_SECTION_LINES) + "\n\n"
    core_tail = ""
    for title, label in (
        ("Preferences & Opinions", "preferences"),
        ("Relationship & Communication", "relationship"),
        ("Key Facts & Context", "key-facts"),
    ):
        core_tail += f"## {title}\n\n" + _filler(label, _CORE_SECTION_LINES) + "\n\n"

    # Fixed scaffolding we always emit, plus the headers of the heavy sections.
    recent_themes = "## Recent Themes\n\n" + _filler("recent-themes", 6) + "\n\n"
    active_header = "## Active Projects\n\n"
    archive_header = "## Archive\n\n"

    fixed = prelude + core_identity + core_tail + active_header + recent_themes + archive_header

    # Distribute the remaining budget between the two heavy sections (Active
    # Projects + Archive), the way a real reflections.md grows: MANY modest H3
    # entries, not a few giant ones. Per-entry size is held ~constant and the
    # COUNT scales with target_chars, so an M3 fold that re-sends the core bundle
    # plus ONE touched entry stays compact at every tier.
    remaining = max(target_chars - len(fixed), 0)
    active_chars = remaining // 2
    archive_chars = remaining - active_chars

    per_section_lines = max(_PER_SECTION_CHARS // 73, 1)
    if n_projects is None:
        n_projects = max(active_chars // _PER_SECTION_CHARS, 1)
    active_body = ""
    for p in range(n_projects):
        active_body += "\n" + _project_subsection(f"project-{p}", per_section_lines) + "\n"

    # Archive as many dated H3 entries (modest each), not one monolithic block.
    n_archive = max(archive_chars // _PER_SECTION_CHARS, 1)
    archive_body = ""
    for a in range(n_archive):
        archive_body += "\n" + _project_subsection(f"archived-{a}", per_section_lines) + "\n"

    return (
        prelude
        + core_identity
        + core_tail
        + active_header
        + active_body
        + "\n"
        + recent_themes
        + archive_header
        + archive_body
    )


def make_observations(target_chars: int, *, start_day: int = 2, n_days: int | None = None) -> str:
    """Synthesize an observations.md of dated ``## YYYY-MM-DD`` sections.

    Spreads ``target_chars`` across consecutive day sections so the chunker
    (which splits on date headers) has realistic multi-day structure to pack into
    folds. ``n_days`` scales with the tier so that, paired with the shrunken
    per-fold chunk budget, the FOLD COUNT grows across 2x/10x/100x (the O(chunks)
    axis). When omitted it defaults to a size-appropriate day count.
    """
    header = "# Observations\n\n"
    if n_days is None:
        # ~6k chars per day keeps day sections realistic; scale day count to size.
        n_days = max((target_chars - len(header)) // 6_000, 1)
    per_day_chars = max((target_chars - len(header)) // n_days, 73)
    per_day_lines = max(per_day_chars // 73, 1)

    body = header
    # Roll across months deterministically so we can emit many distinct date
    # headers (the chunker splits on them) without colliding day numbers.
    months = [(2026, 5, 31), (2026, 6, 30), (2026, 7, 31), (2026, 8, 31), (2026, 9, 30)]
    emitted = 0
    day = start_day
    mi = 0
    while emitted < n_days:
        year, month, ndays = months[mi % len(months)]
        if day > ndays:
            mi += 1
            day = 1
            continue
        date = f"{year:04d}-{month:02d}-{day:02d}"
        body += f"## {date}\n\n" + _filler(f"obs-{date}", per_day_lines) + "\n\n"
        emitted += 1
        day += 1
    return body


@dataclass(frozen=True)
class ScaleScenario:
    """A scale shape plus the shrunken budget that surfaces it deterministically.

    ``multiple`` is the intended growth over the v0.6.7 target document (2x/10x/
    100x). The fixture scales TWO axes so the tiers are behaviorally distinct:
    ``fixture_observations_chars`` (with the paired shrunken ``input_tokens``)
    drives the FOLD COUNT, and ``fixture_reflections_chars`` drives the re-sent
    prefix size. Their product (folds x per-fold resend) is the real
    O(chunks x size) cost and grows monotonically across the tiers.
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
    # The SHRUNKEN per-call ceiling paired with the fixture. Chosen with the
    # observations size so the FOLD COUNT (observations / chunk_budget) and the
    # per-fold reflections prefix both grow across the tiers. Smaller
    # input_tokens at larger observations == more folds AND a smaller per-fold
    # cap, i.e. higher resend complexity.
    input_tokens: int
    # Expected lower bound on fold count at this tier (asserted in the tests so a
    # production chunker change that collapses the tiers fails loudly).
    min_expected_folds: int


# Shrunken-but-faithful scenarios. Byte sizes are small (fast, sub-ms string
# building) but the two scaled axes reproduce 2x/10x/100x pressure honestly:
#
# 2x: reflections ~2x target, generous budget, observations modest -> ~2 folds.
#     The configured 48k cap binds and most reflections context survives; the
#     near-term v0.6.7 knobs handle it.
# 10x: observations large + shrunken budget -> ~24 folds; per-fold cap clamps to
#      ~12k. Full-document resend drops the majority of context every fold and
#      the chunk count is materially higher than 2x.
# 100x: observations larger + a tighter budget -> ~160 folds; per-fold cap clamps
#       to ~5k. The O(chunks x prefix) cost is dramatically higher than 10x along
#       BOTH axes (more folds AND it would need the whole doc per fold) — only a
#       section-targeted strategy keeps per-fold size proportional to touched
#       sections rather than whole-document size.
SCENARIOS: dict[str, ScaleScenario] = {
    "2x": ScaleScenario(
        name="2x",
        multiple=2,
        real_reflections_chars=2 * TARGET_REFLECTIONS_CHARS,  # ~96k
        real_observations_chars=1_500_000,
        fixture_reflections_chars=2 * TARGET_REFLECTIONS_CHARS,  # ~96k (cheap)
        fixture_observations_chars=120_000,
        input_tokens=45_000,  # v0.6.7 default; configured 48k cap binds
        min_expected_folds=2,
    ),
    "10x": ScaleScenario(
        name="10x",
        multiple=10,
        real_reflections_chars=10 * TARGET_REFLECTIONS_CHARS,  # ~480k
        real_observations_chars=7_500_000,
        # Grow reflections ~5x over 2x AND grow observations 5x so the fold count
        # climbs well above 2x. Shrink the budget to clamp the per-fold cap.
        fixture_reflections_chars=10 * TARGET_REFLECTIONS_CHARS,  # ~480k
        fixture_observations_chars=600_000,
        input_tokens=12_000,
        min_expected_folds=10,
    ),
    "100x": ScaleScenario(
        name="100x",
        multiple=100,
        real_reflections_chars=100 * TARGET_REFLECTIONS_CHARS,  # ~4.8M
        real_observations_chars=75_000_000,
        # Grow BOTH axes again over 10x: a much larger reflections document AND
        # 4x the observations against an even tighter budget, so the fold count
        # and the would-be per-fold resend are both dramatically higher than 10x
        # — distinct behavior, not 10x run twice. Still no real-corpus-scale
        # alloc (this builds in sub-ms).
        fixture_reflections_chars=40 * TARGET_REFLECTIONS_CHARS,  # ~1.9M
        fixture_observations_chars=2_400_000,
        input_tokens=7_000,
        min_expected_folds=80,
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

    This is one scale knob (the prefix axis): ratio ~1 means a fold can hold the
    document; ratio >> 1 means full-document resend cannot fit and only a
    section-targeted strategy keeps per-fold size bounded. The companion fold
    count (the chunks axis) is asserted via ``min_expected_folds``; their product
    is the real O(chunks x size) cost.
    """
    return scenario.fixture_reflections_chars / per_call_budget_chars(scenario.input_tokens)


def make_scenario(scenario: ScaleScenario) -> tuple[str, str]:
    """Build (reflections, observations) for a scenario.

    We never literally allocate the real-corpus byte counts. We generate the
    small ``fixture_*`` sizes and rely on the shrunken ``input_tokens`` plus the
    scaled observations so the fold count and size-to-budget ratio reproduce the
    scenario's multiple. Tests assert against fold count, per-fold prompt sizes,
    and total resend — not raw real-corpus byte counts.
    """
    return (
        make_reflections(scenario.fixture_reflections_chars),
        make_observations(scenario.fixture_observations_chars),
    )
