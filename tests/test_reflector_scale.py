"""Reflector scaling-contract tests (Milestone 2, issue #71).

These tests encode the *scaling contract* for the reflector as memory grows
2x / 10x / 100x past the v0.6.7 target document size. They are deterministic and
fast: the synthetic fixtures in ``tests/_scale_fixtures.py`` hold the real OM
reflections shape fixed and scale TWO independent axes per tier —

  - observations size (with a paired shrunken per-call budget) so the FOLD COUNT
    genuinely grows across the tiers (the ``O(chunks)`` axis), and
  - reflections size so the re-sent prefix is realistically large —

so the ``O(chunks x reflections-size)`` resend problem is visible at small byte
counts without allocating megabytes. The product (folds x per-fold resend) grows
monotonically across the tiers; that growing total is the cost the milestone
exists to bound.

What each tier asserts:

  - per-call prompt size (system + user) stays under the computed budget for
    EVERY fold, at every scale (this holds today and must keep holding);
  - the fold count grows materially across 2x/10x/100x (distinct tiers, not the
    same run repeated);
  - the diagnostics report the binding limit (configured vs effective cap);
  - 2x: the immediate #65/v0.6.7 fix — appropriately-configured chunked
    reflection keeps the bulk of the reflections context (no silent clamp);
  - 10x and 100x: the FUTURE section-targeted strategy (Milestone 3). The
    contract is that the always-visible core bundle survives in every fold AND
    the touched section (which lives PAST the head cap) is surfaced — NOT
    head-only truncation (legacy re-sends the same head prefix every fold, so
    sections past the cap are never seen) and NOT full-document resend. Legacy
    chunked reflection can only do head-only truncation or a marker, so these are
    marked ``xfail(strict=True)``: CI stays green today, the contract is
    documented, and they FLIP TO PASS when M3 ships.
  - a resend-complexity guard grounded on BOTH per-fold size AND content: a
    section-targeted reflector re-sends a compact core bundle plus the touched
    section (bounded, and proportional to touched sections — not the whole
    document, not a fixed head prefix), and a separate test proves the legacy
    total resend grows with the corpus (O(chunks x prefix)).

LLM ``compress`` is mocked everywhere — no network. The mock mirrors the
``fake_compress`` capture pattern in ``tests/test_cost_latency.py``.
"""

from __future__ import annotations

import pytest

from observational_memory import reflect
from observational_memory.config import Config
from observational_memory.reflect import _CHARS_PER_TOKEN, _reflect_chunked, _reflector_budgets
from tests._scale_fixtures import (
    CHARS_PER_TOKEN,
    SCENARIOS,
    ScaleScenario,
    make_scenario,
    per_call_budget_chars,
    size_to_budget_ratio,
)

# The always-visible "core bundle" a section-targeted reflector (M3) must keep
# in every fold's context regardless of which observations it is folding, so the
# model never loses durable identity/preferences/facts while patching a section.
# It is deliberately small in the fixtures (each ~900 chars), the way a real
# reflections.md core bundle is, so M3 can carry it in every fold cheaply.
CORE_BUNDLE_HEADERS = (
    "## Core Identity",
    "## Preferences & Opinions",
    "## Relationship & Communication",
    "## Key Facts & Context",
)

# A realistic reflector system-prompt size (matches test_cost_latency.py).
SYSTEM_PROMPT = "S" * 4312


@pytest.fixture(autouse=True)
def _clear_reflector_env(monkeypatch):
    for key in (
        "OM_REFLECTOR_CONTEXT_MAX_CHARS",
        "OM_REFLECTOR_MAX_INPUT_TOKENS",
        "OM_REFLECTOR_OBSERVATION_CHUNK_RATIO",
    ):
        monkeypatch.delenv(key, raising=False)


def _run_chunked_capture(scenario: ScaleScenario, monkeypatch):
    """Run the chunked reflector against a scenario, capturing every fold.

    Mirrors test_cost_latency.py's fake_compress: capture (system_prompt,
    user_content) per fold and return a running document the size of the
    scenario's reflections fixture (so the running doc the next fold would
    re-send is realistically large — the O(chunks x size) case under test).
    """
    reflections, observations = make_scenario(scenario)
    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        return reflections  # running document stays full-size, like the real one

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config(reflector_max_input_tokens=scenario.input_tokens)
    _reflect_chunked(SYSTEM_PROMPT, reflections, observations, cfg)
    assert len(captured) >= 1
    return reflections, observations, captured, cfg


def _reflections_blocks(captured: list[tuple[str, str]]) -> list[str]:
    """The reflections-context block (before the '---' separator) of each fold."""
    return [user_content.split("\n\n---\n\n", 1)[0] for _, user_content in captured]


def _block_headers(block: str) -> set[str]:
    return {line for line in block.splitlines() if line.startswith("## ") or line.startswith("### ")}


def _surfaced_beyond_head(captured: list[tuple[str, str]]) -> set[str]:
    """Headers re-sent on SOME fold that are NOT in the fixed head prefix.

    Legacy chunked reflection re-sends the same capped head prefix on every fold
    (the first fold's context block, before any document growth), so the set of
    headers it ever surfaces is exactly that head prefix — it surfaces NOTHING
    beyond the head cap. A section-targeted M3 fold surfaces the touched entry
    (deep in Active Projects or in Archive, past the head cap) on the fold that
    touches it, so this set is non-empty for M3 and empty for legacy.

    This is the right distinguisher: we do NOT require EVERY section past the head
    to be surfaced (a real M3 only re-sends the sections its observation chunks
    actually touch, and there are more sections than folds), only that targeting
    reaches PAST the fixed head prefix at all — which head-only truncation never
    does.
    """
    blocks = _reflections_blocks(captured)
    head_prefix_headers = _block_headers(blocks[0]) if blocks else set()
    surfaced: set[str] = set()
    for block in blocks:
        surfaced |= _block_headers(block)
    return surfaced - head_prefix_headers


# --------------------------------------------------------------------------- #
# Fixture / scaling-knob sanity (prove the fixtures are not "fake comfort").
# --------------------------------------------------------------------------- #


def test_fixture_chars_per_token_matches_production():
    # If production changes its chars/token estimate, the fixture budget math
    # would silently desync — fail loudly instead.
    assert CHARS_PER_TOKEN == _CHARS_PER_TOKEN


def test_scenarios_have_monotonically_increasing_pressure():
    # The whole point of 2x/10x/100x is increasing size-to-budget pressure. If
    # they collapsed to the same ratio the tiers would be meaningless.
    r2 = size_to_budget_ratio(SCENARIOS["2x"])
    r10 = size_to_budget_ratio(SCENARIOS["10x"])
    r100 = size_to_budget_ratio(SCENARIOS["100x"])
    assert r2 < 1.0, "2x: a fold can still hold ~the document"
    assert r10 > 2.0, "10x: a fold holds well under half the document"
    assert r100 > r10 * 3, "100x: dramatically more pressure than 10x"


@pytest.mark.parametrize(
    ("smaller", "larger"),
    [("2x", "10x"), ("10x", "100x")],
)
def test_fold_count_scales_with_tier(smaller, larger, monkeypatch):
    # The chunks axis: higher tiers must produce materially MORE folds, not the
    # same run repeated. (The earlier single-fixture design pinned observations
    # at 120k for all tiers, so fold count never moved between 10x and 100x — the
    # 100x tier was 10x run twice. This guards against that regression.)
    _rs, _os, cap_s, _cs = _run_chunked_capture(SCENARIOS[smaller], monkeypatch)
    _rl, _ol, cap_l, _cl = _run_chunked_capture(SCENARIOS[larger], monkeypatch)
    folds_s, folds_l = len(cap_s), len(cap_l)
    assert folds_s >= SCENARIOS[smaller].min_expected_folds, (
        f"{smaller}: {folds_s} folds < expected >= {SCENARIOS[smaller].min_expected_folds}"
    )
    assert folds_l >= SCENARIOS[larger].min_expected_folds, (
        f"{larger}: {folds_l} folds < expected >= {SCENARIOS[larger].min_expected_folds}"
    )
    # Higher tier folds at least ~2x more (genuinely distinct, not a paper ratio).
    assert folds_l >= folds_s * 2, (
        f"{larger} ({folds_l} folds) should fold materially more than {smaller} ({folds_s}); "
        "the tiers must exercise distinct chunk counts"
    )


def test_fixtures_have_real_om_structure():
    reflections, observations = make_scenario(SCENARIOS["2x"])
    for header in CORE_BUNDLE_HEADERS:
        assert header in reflections
    assert "## Active Projects" in reflections
    assert "### project-0" in reflections  # per-project H3 subsection
    assert "### archived-0" in reflections  # dated archive H3 entry
    assert "## Recent Themes" in reflections
    assert "## Archive" in reflections
    # The durable core bundle is SMALL and near the top; the bulk is Active
    # Projects + Archive (the real OM distribution), so the core bundle is not an
    # unrealistically huge block and a section-targeted fold can carry it cheaply.
    core_start = reflections.index("## Core Identity")
    active_start = reflections.index("## Active Projects")
    core_bundle_chars = active_start - core_start
    assert core_bundle_chars < 6_000, (
        f"core bundle is {core_bundle_chars} chars; real core sections are modest, "
        "the bulk should live in Active Projects + Archive"
    )
    # Head order: durable identity/core precede Archive (so head-only truncation
    # is observably "drops the heavy tail sections").
    assert reflections.index("## Core Identity") < reflections.index("## Archive")
    assert observations.startswith("# Observations")
    assert "## 2026-05-" in observations  # dated observation sections


# --------------------------------------------------------------------------- #
# Holds at EVERY scale today and must keep holding: per-fold budget ceiling.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario_name", ["2x", "10x", "100x"])
def test_per_call_prompt_stays_under_budget(scenario_name, monkeypatch):
    scenario = SCENARIOS[scenario_name]
    _r, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    budget = per_call_budget_chars(scenario.input_tokens)
    for system_prompt, user_content in captured:
        assert len(system_prompt) + len(user_content) <= budget, (
            f"{scenario_name}: fold exceeded budget {len(system_prompt) + len(user_content)} > {budget}"
        )


def test_diagnostics_report_binding_limit(monkeypatch, caplog):
    # At 10x the input ceiling clamps the effective reflections cap below the
    # configured cap. The system must report BOTH so the operator sees which
    # ceiling binds — not silently blame OM_REFLECTOR_CONTEXT_MAX_CHARS.
    # Asserts the LEGACY head-clamp diagnostics, so pin to the legacy strategy:
    # under the new auto default this corpus would route to sectioned (which does
    # not emit the legacy clamp warning).
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", "legacy")
    scenario = SCENARIOS["10x"]
    with caplog.at_level("WARNING", logger="observational_memory.reflect"):
        _run_chunked_capture(scenario, monkeypatch)
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "configured_reflections_cap=48000" in warnings
    assert "effective_reflections_cap=" in warnings
    # The two must differ (the clamp is real), so the operator isn't told the
    # cap they never set is the binding one.
    assert "configured_reflections_cap=48000 effective_reflections_cap=48000" not in warnings
    assert f"max_input_tokens={scenario.input_tokens}" in warnings


# --------------------------------------------------------------------------- #
# 2x: the immediate #65 / v0.6.7 fix works — chunked reflection without dropping
# most of the reflections context.
# --------------------------------------------------------------------------- #


def test_2x_keeps_bulk_of_reflections_context(monkeypatch):
    # This asserts the LEGACY v0.6.7 chunked ceiling (keep ~48k of context, not
    # the old ~12k clamp). Under the corrected auto threshold (which compares
    # against the EFFECTIVE ~48k per-fold reflections cap, not the ~157.5k input
    # budget), a ~96k 2x document now routes to sectioned — so pin legacy here to
    # keep testing the ceiling this test was written for, exactly as the
    # companion test_diagnostics_report_binding_limit does.
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", "legacy")
    scenario = SCENARIOS["2x"]
    reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    assert len(captured) >= 2, "2x should still take the chunked path"
    blocks = _reflections_blocks(captured)
    max_block = max(len(b) for b in blocks)
    # The configured 48k cap binds (not the old ~12k input-ceiling clamp): the
    # re-sent context keeps ~48k chars, about half of the ~90k doc.
    assert max_block >= 40_000, (
        f"2x reflections context dropped to {max_block} chars; the v0.6.7 ceiling "
        "should keep ~48k, not the old ~12k clamp"
    )
    # And the durable core bundle survives at 2x.
    biggest = max(blocks, key=len)
    for header in CORE_BUNDLE_HEADERS:
        assert header in biggest, f"2x dropped core section {header!r}"


# --------------------------------------------------------------------------- #
# 10x / 100x: the FUTURE section-targeted strategy (Milestone 3). These encode
# the contract and are xfail(strict) until M3 ships. They must NOT bless
# head-only truncation (which legacy does: it re-sends the same capped head
# prefix every fold, so sections past the cap are never seen) or marker-only /
# full-document resend. Do NOT weaken to pass today; do NOT skip (that hides it).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_large_scale_surfaces_touched_sections_not_head_only(scenario_name, monkeypatch):
    # M3 contract: each fold's reflections context carries the touched section
    # (the project/archive entry the folded observations actually concern), so
    # ACROSS the folds the per-fold contexts reach PAST the fixed head prefix.
    # Legacy re-sends the SAME capped head prefix on every fold, so it surfaces
    # NOTHING beyond the head cap (most of Active Projects + all of Archive stay
    # invisible to the reflector). The fixture's core bundle is small enough that
    # head-only truncation keeps it (see the companion test) — so "keep the core
    # bundle" alone would NOT distinguish M3 from head truncation; the
    # distinguishing requirement is that some touched section beyond the head is
    # surfaced. Legacy surfaces none, so this fails today and flips to pass when
    # section-targeted reflection lands. (We require "some", not "all": a real M3
    # only re-sends the sections its observation chunks touch, and there are more
    # sections than folds — what legacy can NEVER do is reach past the head at
    # all.)
    scenario = SCENARIOS[scenario_name]
    _reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    beyond_head = _surfaced_beyond_head(captured)
    assert beyond_head, (
        f"{scenario_name}: no section beyond the fixed head prefix was ever re-sent; a "
        "section-targeted reflector must surface the touched section per fold, not re-send "
        "the same head prefix every fold"
    )


@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_large_scale_keeps_core_bundle_in_every_fold(scenario_name, monkeypatch):
    # M3 contract: the always-visible core bundle (identity, preferences,
    # relationship, key facts) must ride along in EVERY fold's context. This is
    # PAIRED with test_large_scale_surfaces_touched_sections_not_head_only (which
    # rejects head-only truncation): together they require "compact durable core
    # + the touched section", not "fixed head prefix". On its own this test would
    # be satisfiable by head-only truncation (the small core bundle sits at the
    # top), so it is NOT a standalone safety net — the touched-section test is.
    # It fails today because legacy at these tiers must combine a marker/head
    # clamp with a per-fold resend bound that the touched-section coverage breaks.
    scenario = SCENARIOS[scenario_name]
    _reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)
    # M3 must keep the core bundle in every fold AND surface touched sections
    # past the head — encode both here so this test cannot pass via head-only
    # truncation (which keeps the small core bundle but surfaces nothing else).
    beyond_head = _surfaced_beyond_head(captured)
    for i, block in enumerate(blocks):
        missing = [h for h in CORE_BUNDLE_HEADERS if h not in block]
        assert not missing, f"{scenario_name} fold {i} dropped core sections: {missing}"
    assert beyond_head, (
        f"{scenario_name}: core bundle present but no section past the head prefix was ever "
        "surfaced — that is head-only truncation, not section targeting"
    )


@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_large_scale_context_is_section_targeted_not_full_or_head(scenario_name, monkeypatch):
    # M3 contract: per-fold reflections context is proportional to the TOUCHED
    # sections (a compact core bundle + the matching entry), which is much
    # smaller than the whole document but still complete for those sections.
    # Legacy can only (a) resend a capped head prefix that silently drops whole
    # tail sections, or (b) send a marker — neither is "the relevant sections,
    # intact". We require: the largest fold's context carries the core bundle,
    # is materially smaller than the full document (targeting, not full resend),
    # AND surfaces at least one section beyond the head cap (the touched entry,
    # which legacy never re-sends).
    scenario = SCENARIOS[scenario_name]
    reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)
    biggest = max(blocks, key=len)
    # Smaller than the full document (not a full resend)...
    assert len(biggest) < len(reflections) * 0.9
    # ...still carries every core section intact (not head-only / marker)...
    for header in CORE_BUNDLE_HEADERS:
        assert header in biggest, f"{scenario_name}: section-targeting dropped {header!r}"
    # ...and surfaces sections past the head prefix somewhere (the touched entries).
    assert _surfaced_beyond_head(captured), (
        f"{scenario_name}: nothing past the head prefix was ever re-sent — head-only truncation, not section targeting"
    )


# --------------------------------------------------------------------------- #
# Resend-complexity guard. The cost the milestone exists to bound is
# O(folds x per-fold reflections resend). We assert it on TWO axes:
#
#   1. (passes today, documents the problem) the TOTAL resend across folds grows
#      with the corpus under legacy — folds scale with observations and each fold
#      re-sends a large prefix, so the product climbs steeply 2x -> 10x -> 100x.
#      A scaling-safe strategy must break that growth.
#   2. (xfail until M3) a section-targeted reflector re-sends only a compact core
#      bundle PLUS the touched section per fold, so per-fold resend is bounded by
#      a small constant AND the touched section (past the head) is present. The
#      size bound alone is NOT enough (at 100x the legacy head clamp is already
#      small), so the guard ALSO requires the beyond-head section coverage — so
#      "small" cannot be achieved by dropping durable/tail sections (head-only
#      truncation), the exact anti-pattern the milestone forbids.
# --------------------------------------------------------------------------- #

# A section-targeted fold carries the compact core bundle (~3.6k) plus one
# touched entry (~4k) plus wrappers — comfortably under this. Legacy's per-fold
# resend at 10x (~12k effective cap) sits above it; at 100x legacy's per-fold
# size is below it, which is why the guard ALSO checks content (touched-section
# coverage), so head-only truncation cannot satisfy it.
SECTION_TARGETED_RESEND_CEILING = 9_000


def estimate_resend_chars(blocks: list[str]) -> int:
    """Total chars of reflections context re-sent across all folds."""
    return sum(len(b) for b in blocks)


def resend_per_fold_chars(blocks: list[str]) -> float:
    """Average reflections-context chars re-sent per fold."""
    return estimate_resend_chars(blocks) / max(len(blocks), 1)


def test_resend_guard_2x_is_acceptable(monkeypatch):
    # At 2x the per-fold resend is bounded by the configured 48k cap with only a
    # couple folds — acceptable for the near-term knobs fix. This documents the
    # regime where the legacy strategy is still fine.
    scenario = SCENARIOS["2x"]
    _r, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)
    assert resend_per_fold_chars(blocks) <= 48_000 + 200  # cap + marker slack


def test_total_resend_grows_with_corpus_today(monkeypatch):
    # Smoking gun (passes today, documents the O(chunks x prefix) problem): the
    # TOTAL reflections resend (sum over folds = folds x per-fold prefix) grows
    # steeply as the corpus grows, because folds scale with observations and each
    # fold re-sends a large prefix. This is the cost M3 must bound. We assert it
    # grows monotonically AND super-linearly across the tiers so a future "fix"
    # that merely shrinks the per-fold prefix while leaving folds x prefix large
    # is still caught.
    #
    # This documents LEGACY behavior (the O(chunks x prefix) problem M3 bounds),
    # so pin to the legacy strategy — under the new auto default 10x/100x route to
    # sectioned, whose total resend does NOT grow this way (that is the whole point).
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", "legacy")
    totals = {}
    for name in ("2x", "10x", "100x"):
        _r, _o, captured, _cfg = _run_chunked_capture(SCENARIOS[name], monkeypatch)
        totals[name] = estimate_resend_chars(_reflections_blocks(captured))
    assert totals["10x"] > totals["2x"] * 2, f"total resend should climb 2x->10x: {totals}"
    assert totals["100x"] > totals["10x"] * 2, (
        f"total resend should climb 10x->100x (more folds AND a larger document): {totals}"
    )


def test_per_fold_resend_does_not_shrink_to_a_targeted_bundle_today(monkeypatch):
    # Companion to the xfail guard: today, at 10x, legacy re-sends a large fixed
    # head prefix every fold (well above a section-targeted bundle), so per-fold
    # resend does NOT collapse to the compact core+section size a scaling-safe
    # strategy would use. Documents the legacy behavior the M3 guard requires be
    # eliminated. (At 100x the effective cap is itself small, which is exactly why
    # the M3 guard pairs the size bound with touched-section coverage — see
    # test_resend_complexity_guard_large_scale.)
    #
    # Documents LEGACY per-fold resend, so pin to the legacy strategy — the auto
    # default would route this corpus to sectioned (the compact bundle this test
    # asserts legacy does NOT collapse to).
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", "legacy")
    _r, _o, cap10, _c = _run_chunked_capture(SCENARIOS["10x"], monkeypatch)
    resend10 = resend_per_fold_chars(_reflections_blocks(cap10))
    assert resend10 > SECTION_TARGETED_RESEND_CEILING, (
        f"10x per-fold resend {resend10:.0f} should exceed the section-targeted ceiling "
        f"{SECTION_TARGETED_RESEND_CEILING} under legacy (large fixed head prefix every fold)"
    )


@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_resend_complexity_guard_large_scale(scenario_name, monkeypatch):
    # The guard: a section-targeted reflector re-sends only a compact core bundle
    # (plus the touched section) per fold, so per-fold resend stays a small
    # constant INDEPENDENT of document size — AND the touched section (past the
    # head cap) is present, so "small" cannot be achieved by head-only truncation
    # (dropping durable/tail sections). Both clauses are required: at 100x the
    # legacy effective cap is already below the size ceiling, so the size bound
    # alone would be (wrongly) satisfied by head-only truncation; the content
    # clause is what makes the guard reject that anti-pattern.
    #
    # Legacy at 10x re-sends ~the large effective-cap head prefix every fold
    # (blows the size bound); at 100x it re-sends a smaller fixed head prefix that
    # NEVER carries any section past the head cap (fails the content clause). It
    # is xfail until M3 makes per-fold resend proportional to touched sections.
    scenario = SCENARIOS[scenario_name]
    _reflections, _o, captured, cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)

    reflections_cap, _chunk_budget = _reflector_budgets(SYSTEM_PROMPT, "", cfg)
    avg_resend = resend_per_fold_chars(blocks)

    # Clause 1: per-fold resend is a small constant (compact core + touched entry).
    assert avg_resend <= SECTION_TARGETED_RESEND_CEILING, (
        f"{scenario_name}: chunked reflection re-sends ~{avg_resend:.0f} chars/fold "
        f"(effective cap {reflections_cap}); a section-targeted strategy must keep "
        f"per-fold resend under {SECTION_TARGETED_RESEND_CEILING} chars (proportional "
        "to touched sections, not a large fixed prefix re-sent every fold)"
    )
    # Clause 2: 'small' must come from targeting, NOT head-only truncation — at
    # least one section past the fixed head prefix must be surfaced across folds.
    assert _surfaced_beyond_head(captured), (
        f"{scenario_name}: per-fold resend is small but nothing past the head prefix was "
        "ever re-sent — that is head-only truncation, not section targeting; the guard "
        "rejects shrinking resend by dropping durable/tail sections"
    )
