"""Reflector scaling-contract tests (Milestone 2, issue #71).

These tests encode the *scaling contract* for the reflector as memory grows
2x / 10x / 100x past the v0.6.7 target document size. They are deterministic
and fast: the synthetic fixtures in ``tests/_scale_fixtures.py`` hold the real
OM reflections shape fixed and express the multiple as the ratio of reflections
size to the (shrunken) per-call budget, so the O(chunks x reflections-size)
resend problem is visible at small byte counts without allocating megabytes.

What each tier asserts:

  - per-call prompt size (system + user) stays under the computed budget for
    EVERY fold, at every scale (this holds today and must keep holding);
  - the diagnostics report the binding limit (configured vs effective cap);
  - 2x: the immediate #65/v0.6.7 fix — appropriately-configured chunked
    reflection keeps the bulk of the reflections context (no silent clamp);
  - 10x and 100x: the FUTURE section-targeted strategy (Milestone 3). The
    contract is that the always-visible core bundle survives in every fold and
    per-fold context stays proportional to touched sections — NOT head-only
    truncation (which drops Key Facts / Recent Themes / Archive) and NOT
    full-document resend. Legacy chunked reflection can only truncate the head
    or send a marker, so these are marked ``xfail(strict=True)``: CI stays green
    today, the contract is documented, and they FLIP TO PASS when M3 ships.
  - a resend-complexity guard that fails if chunked reflection re-sends ~the
    same large reflections prefix on every fold at 10x/100x scale.

LLM ``compress`` is mocked everywhere — no network. The mock mirrors the
``fake_compress`` capture pattern in ``tests/test_cost_latency.py``.
"""

from __future__ import annotations

import pytest

from observational_memory import reflect
from observational_memory.config import Config
from observational_memory.reflect import _CHARS_PER_TOKEN, _reflector_budgets
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
    reflect._reflect_chunked(SYSTEM_PROMPT, reflections, observations, cfg)
    assert len(captured) >= 1
    return reflections, observations, captured, cfg


def _reflections_blocks(captured: list[tuple[str, str]]) -> list[str]:
    """The reflections-context block (before the '---' separator) of each fold."""
    return [user_content.split("\n\n---\n\n", 1)[0] for _, user_content in captured]


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


def test_fixtures_have_real_om_structure():
    reflections, observations = make_scenario(SCENARIOS["2x"])
    for header in CORE_BUNDLE_HEADERS:
        assert header in reflections
    assert "## Active Projects" in reflections
    assert "### project-0" in reflections  # per-project H3 subsection
    assert "## Recent Themes" in reflections
    assert "## Archive" in reflections
    # Head order: durable identity/projects precede Archive (so head-only
    # truncation is observably "drops the tail sections").
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
    scenario = SCENARIOS["2x"]
    reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    assert len(captured) >= 2, "2x should still take the chunked path"
    blocks = _reflections_blocks(captured)
    max_block = max(len(b) for b in blocks)
    # The configured 48k cap binds (not the old ~12k input-ceiling clamp): the
    # re-sent context keeps ~48k chars, about half of the ~96k doc.
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
# head-only truncation (which legacy does at 10x) or marker-only / full-document
# resend (100x). Do NOT weaken to pass today; do NOT skip (that hides it).
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(strict=True, reason="section-targeted reflection lands in M3 / #71")
@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_large_scale_keeps_core_bundle_in_every_fold(scenario_name, monkeypatch):
    # M3 contract: the always-visible core bundle (identity, preferences,
    # relationship, key facts) must ride along in EVERY fold's context, even
    # while the document as a whole is far too large to resend. Legacy chunked
    # reflection truncates to the head (dropping Key Facts / later sections) at
    # 10x, or sends a marker only at 100x — so this fails today and flips to
    # pass when section-targeted reflection keeps the core bundle visible.
    scenario = SCENARIOS[scenario_name]
    _r, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)
    for i, block in enumerate(blocks):
        missing = [h for h in CORE_BUNDLE_HEADERS if h not in block]
        assert not missing, f"{scenario_name} fold {i} dropped core sections: {missing}"


@pytest.mark.xfail(strict=True, reason="section-targeted reflection lands in M3 / #71")
@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_large_scale_context_is_section_targeted_not_full_or_head(scenario_name, monkeypatch):
    # M3 contract: per-fold reflections context is proportional to the TOUCHED
    # sections (a compact core bundle + the matching project subsection), which
    # is much smaller than the whole document but still complete for those
    # sections. Legacy can only (a) resend a capped head prefix that silently
    # drops whole tail sections, or (b) send a marker — neither is "the relevant
    # sections, intact". We require: the largest fold's context carries the core
    # bundle AND is materially smaller than the full document (targeting, not
    # full resend).
    scenario = SCENARIOS[scenario_name]
    reflections, _o, captured, _cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)
    biggest = max(blocks, key=len)
    # Smaller than the full document (not a full resend)...
    assert len(biggest) < len(reflections) * 0.9
    # ...but still carries every core section intact (not head-only / marker).
    for header in CORE_BUNDLE_HEADERS:
        assert header in biggest, f"{scenario_name}: section-targeting dropped {header!r}"


# --------------------------------------------------------------------------- #
# Resend-complexity guard. Estimates how much reflections-prefix is re-sent
# across folds. A section-targeted strategy (M3) re-sends only a compact core
# bundle plus the touched section each fold, so per-fold resend is a small
# constant INDEPENDENT of how large reflections.md has grown. Legacy chunked
# reflection re-sends the same large effective-cap prefix on EVERY fold, so its
# per-fold resend is a large constant that does not shrink as the document grows
# — the O(chunks x prefix-size) cost the milestone exists to bound.
# --------------------------------------------------------------------------- #

# A section-targeted fold carries a compact core bundle + one touched section.
# In the fixture that is a few KB; we allow generous headroom. Legacy's per-fold
# resend (the ~12k effective cap) sits above this, so the guard trips today.
SECTION_TARGETED_RESEND_CEILING = 8_000


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


def test_resend_per_fold_does_not_shrink_as_document_grows_today(monkeypatch):
    # Smoking gun (passes today, documents the problem): 100x has a 5x larger
    # reflections document than 10x, yet legacy re-sends the SAME large per-fold
    # prefix (clamped to the effective cap) on every fold. A scaling-safe
    # strategy would make per-fold resend independent of total size in a GOOD
    # way (small); legacy makes it independent in a BAD way (large constant).
    _r10, _o10, cap10, _c10 = _run_chunked_capture(SCENARIOS["10x"], monkeypatch)
    refl100, _o100, cap100, _c100 = _run_chunked_capture(SCENARIOS["100x"], monkeypatch)
    resend10 = resend_per_fold_chars(_reflections_blocks(cap10))
    resend100 = resend_per_fold_chars(_reflections_blocks(cap100))
    # Document is far bigger at 100x...
    refl10, _ = make_scenario(SCENARIOS["10x"])
    assert len(refl100) > len(refl10) * 3
    # ...but the per-fold resend barely changes — both are pinned near the
    # effective cap, well above a section-targeted bundle. That constant-large
    # prefix is exactly what the M3 guard below requires be eliminated.
    assert resend10 > SECTION_TARGETED_RESEND_CEILING
    assert resend100 > SECTION_TARGETED_RESEND_CEILING
    assert abs(resend100 - resend10) <= max(resend10 * 0.5, 1000)


@pytest.mark.xfail(strict=True, reason="section-targeted reflection lands in M3 / #71")
@pytest.mark.parametrize("scenario_name", ["10x", "100x"])
def test_resend_complexity_guard_large_scale(scenario_name, monkeypatch):
    # The guard: a section-targeted reflector re-sends only a compact core
    # bundle (plus the touched section) per fold, so per-fold resend stays a
    # small constant INDEPENDENT of document size. We bound the average per-fold
    # reflections resend by a fixed ceiling that does NOT scale with the (huge)
    # document.
    #
    # Legacy chunked reflection re-sends ~the same large head prefix (the
    # effective reflections cap, ~12k here) on EVERY fold at both 10x and 100x,
    # which blows this fixed bound. It is xfail until M3 makes per-fold resend
    # proportional to touched sections rather than a large fixed prefix.
    scenario = SCENARIOS[scenario_name]
    _reflections, _o, captured, cfg = _run_chunked_capture(scenario, monkeypatch)
    blocks = _reflections_blocks(captured)

    reflections_cap, _chunk_budget = _reflector_budgets(SYSTEM_PROMPT, "", cfg)
    avg_resend = resend_per_fold_chars(blocks)

    assert avg_resend <= SECTION_TARGETED_RESEND_CEILING, (
        f"{scenario_name}: chunked reflection re-sends ~{avg_resend:.0f} chars/fold "
        f"(effective cap {reflections_cap}); a section-targeted strategy must keep "
        f"per-fold resend under {SECTION_TARGETED_RESEND_CEILING} chars (proportional "
        "to touched sections, not a large fixed prefix re-sent every fold)"
    )
