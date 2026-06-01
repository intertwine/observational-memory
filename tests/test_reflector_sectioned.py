"""Section-targeted reflector tests (Milestone 3, issue #71).

These cover the behavioral contract of the sectioned strategy beyond the scale
contracts in ``test_reflector_scale.py``:

  - routing surfaces only the touched section's context, leaving unrelated
    sections out of the per-fold prompt (and untouched on disk byte-for-byte);
  - the per-fold prompt scales with the SELECTED sections, not the full document;
  - a single-project observation updates ONLY that project's content while every
    unrelated section is preserved exactly;
  - invalid / unparseable reflector output FAILS CLOSED — durable memory is left
    unchanged, never partially written;
  - adding a brand-new section/project works via the NEW_AFTER envelope;
  - the strict section-patch parser rejects malformed envelopes.

LLM ``compress`` is mocked everywhere — no network.
"""

from __future__ import annotations

import pytest

from observational_memory import reflect
from observational_memory.config import Config
from observational_memory.reflect import _reflect_sectioned
from observational_memory.reflection_patch import PatchParseError, parse_section_patches
from observational_memory.reflection_router import route_chunk
from observational_memory.reflection_sections import parse_reflection_document

# A small, realistic reflections.md with the OM section structure and two
# distinct project subsections, so single-project targeting is observable.
REFLECTIONS = """\
# Reflections

*Last updated: 2026-05-01 09:00 UTC*
*Last reflected: 2026-05-01*

## Core Identity

- **Name:** Alex
- **Role:** engineer

## Preferences & Opinions

- 🔴 prefers uv over pip

## Relationship & Communication

- terse, no fluff

## Key Facts & Context

- 🔴 works in Pacific time

## Active Projects

### observational-memory

- **Status:** active
- **Stack:** Python
- **Current state:** building the reflector

### widget-factory

- **Status:** paused
- **Stack:** Rust
- **Current state:** on hold pending review

## Recent Themes

- shipping the reflector scaling work

## Archive

### archived-thing

- [2026-01-01] old note
"""

OBSERVATIONS = """\
# Observations

## 2026-05-10

- Worked on observational-memory: landed the section router and patch parser.
- Decided to keep per-fold resend proportional to touched sections.
"""


def _config():
    return Config(reflector_strategy="sectioned", reflector_max_input_tokens=45000)


# --------------------------------------------------------------------------- #
# Routing: only the touched section's context is surfaced.
# --------------------------------------------------------------------------- #


def test_route_includes_core_bundle_always():
    doc = parse_reflection_document(REFLECTIONS)
    route = route_chunk(doc, "unrelated chatter about nothing", fold_index=0, fold_total=1)
    for slug in (
        "ref:core-identity",
        "ref:preferences-opinions",
        "ref:relationship-communication",
        "ref:key-facts-context",
    ):
        assert slug in route.section_handles


def test_route_targets_named_project_subsection_only():
    doc = parse_reflection_document(REFLECTIONS)
    route = route_chunk(
        doc,
        "Worked on observational-memory today: landed the router.",
        fold_index=0,
        fold_total=1,
    )
    # The named project's H3 entry is targeted; the unrelated project is NOT.
    assert "ref:active-projects:observational-memory" in route.subsection_handles
    assert "ref:active-projects:widget-factory" not in route.subsection_handles


def test_current_work_pulls_recent_themes():
    doc = parse_reflection_document(REFLECTIONS)
    route = route_chunk(doc, "currently working on a release today", fold_index=0, fold_total=1)
    assert "ref:recent-themes" in route.section_handles


# A reflections doc with HYPHENATED project names, to exercise abbreviated refs.
REFLECTIONS_HYPHEN = """\
# Reflections

*Last updated: 2026-05-01 09:00 UTC*
*Last reflected: 2026-05-01*

## Core Identity

- **Name:** Alex

## Active Projects

### hermes-agent

- **Status:** active

### om-relay

- **Status:** active

## Recent Themes

- shipping
"""


def test_route_matches_abbreviated_project_name():
    # "hermes" must match the "hermes-agent" H3; "relay" must match "om-relay".
    # Users routinely abbreviate, so a name component must reach its parent entry.
    doc = parse_reflection_document(REFLECTIONS_HYPHEN)
    route = route_chunk(doc, "Spent today on hermes: fixed the gateway auth bug.", fold_index=0, fold_total=1)
    assert "ref:active-projects:hermes-agent" in route.subsection_handles
    route2 = route_chunk(doc, "Poked at the relay health check.", fold_index=0, fold_total=1)
    assert "ref:active-projects:om-relay" in route2.subsection_handles


def test_route_full_hyphenated_name_still_matches():
    doc = parse_reflection_document(REFLECTIONS_HYPHEN)
    route = route_chunk(doc, "Worked on hermes-agent: completed the auth port.", fold_index=0, fold_total=1)
    assert "ref:active-projects:hermes-agent" in route.subsection_handles


def test_new_project_observation_is_not_rotation_routed_as_a_match():
    # A genuinely new repo not in reflections.md must NOT be surfaced as a "real"
    # match. The rotation fallback may surface SOMETHING for coverage, but the
    # route must mark it rotation-only and report the unmatched name token so the
    # caller steers toward NEW_AFTER instead of editing an unrelated sibling.
    doc = parse_reflection_document(REFLECTIONS)
    route = route_chunk(doc, "set up brand-new repo zeta-service with CI", fold_index=1, fold_total=3)
    assert route.rotation_only is True
    assert "zeta-service" in route.unmatched_name_tokens or "zeta" in route.unmatched_name_tokens


def test_rotation_only_fold_offers_no_subsection_as_patchable(monkeypatch):
    # On a rotation-only fold (nothing matched), the arbitrary rotated entry must
    # NOT be advertised as patchable, so the model is not biased to edit it.
    captured: list[str] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append(user_content)
        return user_content  # invalid -> fail closed; we only inspect the prompt

    monkeypatch.setattr(reflect, "compress", fake_compress)
    new_proj_obs = "# Observations\n\n## 2026-05-10\n\n- set up brand-new repo zeta-service with CI\n"
    _reflect_sectioned("SYS", REFLECTIONS, new_proj_obs, _config())
    assert captured
    handles_block = captured[0].rsplit("## Available section handles", 1)[1]
    # No Active Projects child handle is advertised as patchable on this fold.
    assert "ref:active-projects:" not in handles_block
    # And the new-project steering hint is present.
    assert "ADD a new section" in handles_block


# --------------------------------------------------------------------------- #
# Per-fold prompt scales with selected sections, not the full document.
# --------------------------------------------------------------------------- #


def test_per_fold_prompt_scales_with_selected_sections(monkeypatch):
    # Grow the document with many unrelated projects; the per-fold reflections
    # context for a single-project observation must NOT grow with them.
    extra = ""
    for n in range(40):
        extra += f"\n### filler-{n}\n\n- status: parked\n- detail: synthetic project body line for size\n"
    big = REFLECTIONS.replace("## Recent Themes", extra + "\n## Recent Themes")

    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        return user_content  # not a valid envelope -> fail-closed; we only inspect input

    monkeypatch.setattr(reflect, "compress", fake_compress)
    _reflect_sectioned("SYS", big, OBSERVATIONS, _config())
    assert captured
    block = captured[0][1].split("\n\n---\n\n", 1)[0]
    # The reflections context is a small fraction of the (now large) document.
    assert len(block) < len(big) * 0.5
    # And it carries the core bundle.
    for header in ("## Core Identity", "## Preferences & Opinions"):
        assert header in block


# --------------------------------------------------------------------------- #
# Single-project update touches ONLY that project; everything else byte-faithful.
# --------------------------------------------------------------------------- #


def test_single_project_update_preserves_unrelated_sections_byte_for_byte(monkeypatch):
    doc = parse_reflection_document(REFLECTIONS)

    # The model adds a NEW project section (the safe v1 path for project updates),
    # leaving every existing section untouched.
    def fake_compress(system_prompt, user_content, config, **kwargs):
        return (
            "SECTION_HANDLE: ref:new-insight\n"
            "NEW_AFTER: ref:recent-themes\n"
            "UPDATED_MARKDOWN:\n"
            "## New Insight\n\n- section router shipped\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())

    out = parse_reflection_document(result)
    # The new section exists...
    assert out.section_by_handle("ref:new-insight") is not None
    # ...and every original section is preserved byte-for-byte.
    for section in doc.sections:
        kept = out.section_by_handle(section.handle)
        assert kept is not None, f"dropped section {section.handle}"
        assert kept.text == section.text, f"section {section.handle} not byte-identical"


def test_core_section_patch_preserves_other_sections(monkeypatch):
    doc = parse_reflection_document(REFLECTIONS)

    def fake_compress(system_prompt, user_content, config, **kwargs):
        return (
            "SECTION_HANDLE: ref:core-identity\n"
            "UPDATED_MARKDOWN:\n"
            "## Core Identity\n\n- **Name:** Alex\n- **Role:** staff engineer\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    out = parse_reflection_document(result)
    # Core Identity changed...
    assert "staff engineer" in out.section_by_handle("ref:core-identity").text
    # ...the heavy Active Projects section is untouched byte-for-byte.
    assert out.section_by_handle("ref:active-projects").text == doc.section_by_handle("ref:active-projects").text


# --------------------------------------------------------------------------- #
# Fail-closed: invalid reflector output leaves memory unchanged.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_output",
    [
        "here is your updated document, totally not an envelope",
        "SECTION_HANDLE: ref:core-identity\nUPDATED_MARKDOWN:\nno header line at all",
        "SECTION_HANDLE: ref:core-identity\nUPDATED_MARKDOWN:\n## A\n## B\n",  # two headers
        "",
    ],
)
def test_invalid_output_leaves_memory_unchanged(bad_output, monkeypatch):
    def fake_compress(system_prompt, user_content, config, **kwargs):
        return bad_output

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    # Single chunk, invalid output -> running document is exactly the input.
    assert result == REFLECTIONS


def test_patch_for_unknown_handle_fails_closed(monkeypatch):
    def fake_compress(system_prompt, user_content, config, **kwargs):
        return "SECTION_HANDLE: ref:does-not-exist\nUPDATED_MARKDOWN:\n## Ghost\n- nope\n"

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    assert result == REFLECTIONS


def test_patch_for_known_but_not_offered_heavy_h2_fails_closed(monkeypatch):
    # The corruption Validation gate 3 forbids: the model echoes back a heavy H2
    # (## Active Projects) it was only shown ONE H3 of, restating just one child.
    # Active Projects is a KNOWN handle but was NOT in the offered patchable set,
    # so the fold must FAIL CLOSED — siblings widget-factory and the rest survive.
    def fake_compress(system_prompt, user_content, config, **kwargs):
        return (
            "SECTION_HANDLE: ref:active-projects\n"
            "UPDATED_MARKDOWN:\n"
            "## Active Projects\n\n"
            "### observational-memory\n\n- only this one survives, the rest are dropped\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    # Unchanged on disk: the not-offered heavy-H2 patch was rejected.
    assert result == REFLECTIONS
    out = parse_reflection_document(result)
    active = out.section_by_handle("ref:active-projects")
    assert "widget-factory" in active.text


def test_full_section_patch_dropping_h3_children_fails_closed(monkeypatch):
    # If a full-section patch is somehow offered for a section that HAS H3 children
    # and the patch drops one, fail closed rather than lose the dropped entry.
    # Build a doc where Active Projects is a SMALL section so routing offers it
    # whole, with two children; the model returns it minus one child.
    small_active = """\
# Reflections

*Last updated: 2026-05-01 09:00 UTC*
*Last reflected: 2026-05-01*

## Core Identity

- **Name:** Alex

## Active Projects

### keep-me

- **Status:** active

### drop-me

- **Status:** active
"""
    # Force Active Projects to be offered as a full patchable section by patching
    # the apply path directly: route would normally surface H3s, so test the guard
    # at the apply layer where the offered set includes the H2.
    from observational_memory.reflect import _apply_section_patches
    from observational_memory.reflection_patch import parse_section_patches

    doc = parse_reflection_document(small_active)
    raw = (
        "SECTION_HANDLE: ref:active-projects\n"
        "UPDATED_MARKDOWN:\n"
        "## Active Projects\n\n### keep-me\n\n- **Status:** active\n"
    )
    patches = parse_section_patches(raw)
    # Offer the H2 as patchable (simulating a small-section route).
    result = _apply_section_patches(doc, patches, ["ref:active-projects"])
    assert result is None  # dropped 'drop-me' -> fail closed


def test_in_place_subsection_update_updates_only_that_project(monkeypatch):
    # An exact-name observation must be able to UPDATE the existing project H3 in
    # place — not just add a duplicate top-level section. The router surfaces the
    # H3 as patchable; the model patches it; siblings are preserved byte-for-byte.
    doc = parse_reflection_document(REFLECTIONS)

    def fake_compress(system_prompt, user_content, config, **kwargs):
        # The H3 handle must be advertised as patchable.
        assert "ref:active-projects:observational-memory" in user_content
        return (
            "SECTION_HANDLE: ref:active-projects:observational-memory\n"
            "UPDATED_MARKDOWN:\n"
            "### observational-memory\n\n"
            "- **Status:** active\n- **Current state:** section router shipped\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    out = parse_reflection_document(result)
    active = out.section_by_handle("ref:active-projects")
    assert "section router shipped" in active.text
    # The sibling project is untouched.
    assert "widget-factory" in active.text
    assert "on hold pending review" in active.text
    # No duplicate Active Projects section was created.
    assert out.handles().count("ref:active-projects") == 1
    # Every other section byte-identical.
    for section in doc.sections:
        if section.handle == "ref:active-projects":
            continue
        assert out.section_by_handle(section.handle).text == section.text


# --------------------------------------------------------------------------- #
# Adding a new section/project works.
# --------------------------------------------------------------------------- #


def test_add_new_section_at_end(monkeypatch):
    def fake_compress(system_prompt, user_content, config, **kwargs):
        return "SECTION_HANDLE: ref:new-area\nNEW_AFTER:\nUPDATED_MARKDOWN:\n## New Area\n\n- fresh content\n"

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = _reflect_sectioned("SYS", REFLECTIONS, OBSERVATIONS, _config())
    out = parse_reflection_document(result)
    assert out.sections[-1].heading == "New Area"
    assert "fresh content" in out.sections[-1].text


# --------------------------------------------------------------------------- #
# Strict parser unit tests.
# --------------------------------------------------------------------------- #


def test_parser_accepts_multiple_patches():
    raw = (
        "SECTION_HANDLE: ref:core-identity\nUPDATED_MARKDOWN:\n## Core Identity\n- a\n\n"
        "SECTION_HANDLE: ref:key-facts-context\nUPDATED_MARKDOWN:\n## Key Facts & Context\n- b\n"
    )
    patches = parse_section_patches(raw)
    assert [p.handle for p in patches] == ["ref:core-identity", "ref:key-facts-context"]


def test_parser_rejects_duplicate_handle():
    raw = (
        "SECTION_HANDLE: ref:core-identity\nUPDATED_MARKDOWN:\n## Core Identity\n- a\n\n"
        "SECTION_HANDLE: ref:core-identity\nUPDATED_MARKDOWN:\n## Core Identity\n- b\n"
    )
    with pytest.raises(PatchParseError):
        parse_section_patches(raw)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "no marker",
        "SECTION_HANDLE:\nUPDATED_MARKDOWN:\n## H\n- x",  # empty handle
        "SECTION_HANDLE: ref:x",  # missing UPDATED_MARKDOWN
        "SECTION_HANDLE: ref:x\nUPDATED_MARKDOWN:\n",  # empty markdown
        "junk before\nSECTION_HANDLE: ref:x\nUPDATED_MARKDOWN:\n## H\n- x",  # leading prose
    ],
)
def test_parser_fails_closed_on_malformed(bad):
    with pytest.raises(PatchParseError):
        parse_section_patches(bad)


def test_parser_does_not_split_on_section_handle_inside_markdown_body():
    # OM's own memory/docs describe the envelope, so a reflection ABOUT this
    # feature can contain a body line starting with "SECTION_HANDLE:". That line
    # must stay part of the ONE patch's markdown, not synthesize a second patch
    # that truncates the real section. (A body line beginning with the marker has
    # no UPDATED_MARKDOWN of its own, so it is re-joined to its patch.)
    raw = (
        "SECTION_HANDLE: ref:core-identity\n"
        "UPDATED_MARKDOWN:\n"
        "## Core Identity\n\n"
        "- The reflector emits an envelope:\n"
        "SECTION_HANDLE: ref:archive\n"
        "- ...and then the markdown body.\n"
    )
    patches = parse_section_patches(raw)
    assert len(patches) == 1
    assert patches[0].handle == "ref:core-identity"
    # The injected line stays inside the body, NOT a second patch.
    assert "SECTION_HANDLE: ref:archive" in patches[0].markdown
    assert "...and then the markdown body." in patches[0].markdown


# --------------------------------------------------------------------------- #
# Gate 3: section provenance survives the M3 sectioned reflect end-to-end.
# --------------------------------------------------------------------------- #


def test_sectioned_reflector_restamps_after_patch(monkeypatch, tmp_path):
    """Run the M3 sectioned path end-to-end through run_reflector ->
    finalize_reflection. On a FIRST stamping run (the on-disk fixture carries no
    per-bullet `<!--om:-->` metadata yet), ensure_reflection_metadata adds that
    metadata to every section, so every section's body changes and every H2 is
    stamped exactly once; the doc must still round-trip byte-for-byte and a model
    patch that omits the stamp gets deterministically re-stamped."""
    from observational_memory.config import Config
    from observational_memory.reflect import run_reflector
    from observational_memory.reflection_sections import parse_reflection_document

    config = Config(
        memory_dir=tmp_path / "memory",
        reflector_strategy="sectioned",
        reflector_max_input_tokens=45000,
    )
    config.ensure_memory_dir()
    config.reflections_path.write_text(REFLECTIONS)
    config.observations_path.write_text(OBSERVATIONS)

    def fake_compress(system_prompt, user_content, config, **kwargs):
        # The model patches one section and does NOT emit any section stamp.
        return (
            "SECTION_HANDLE: ref:core-identity\n"
            "UPDATED_MARKDOWN:\n"
            "## Core Identity\n\n- **Name:** Alex\n- **Role:** staff engineer\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = run_reflector(config, dry_run=True)
    assert result is not None

    parsed = parse_reflection_document(result)
    assert parsed.sections, "expected H2 sections in the reassembled doc"
    # First-run: every section changes (per-bullet metadata added) and is stamped.
    for section in parsed.sections:
        assert "<!--om-section:" in section.text, f"section {section.handle} missing stamp"
    # Exactly one stamp per H2 (no duplication from the patch path).
    assert result.count("<!--om-section:") == len(parsed.sections)
    # The stamped document still round-trips byte-for-byte.
    assert parsed.render() == result


def test_sectioned_untouched_section_keeps_prior_last_reflected(monkeypatch, tmp_path):
    """Gate 3 honesty: in the sectioned path an UNTOUCHED section must keep its
    PRIOR `last_reflected` (it was not folded this run) while only the touched
    section is restamped with today's date. A blanket restamp would lie about the
    freshness of every untouched section at scale."""
    from observational_memory.config import Config
    from observational_memory.reflect import run_reflector

    # Prior is already in steady state: per-bullet metadata + section markers
    # dated 2026-05-09 on every section.
    prior = (
        "# Reflections\n\n"
        "*Last updated: 2026-05-09 09:00 UTC*\n"
        "*Last reflected: 2026-05-09*\n\n"
        "## Core Identity\n\n"
        "<!--om-section: last_reflected=2026-05-09 derived_from_obs_window=2026-05-08..2026-05-09-->\n"
        "- **Name:** Alex <!--om: id=ome_aaa kind=identity source_type=inferred confidence=medium "
        "sensitivity=personal actionability=high last_seen=2026-05-09T00:00:00Z seen_count=1 "
        "node=local scope=cluster-->\n\n"
        "## Preferences & Opinions\n\n"
        "<!--om-section: last_reflected=2026-05-09 derived_from_obs_window=2026-05-08..2026-05-09-->\n"
        "- prefers uv over pip <!--om: id=ome_bbb kind=preference source_type=inferred confidence=medium "
        "sensitivity=normal actionability=medium last_seen=2026-05-09T00:00:00Z seen_count=1 "
        "node=local scope=cluster-->\n"
    )
    observations = "# Observations\n\n## 2026-05-10\n\n- Worked on observational-memory: landed the router.\n"

    config = Config(
        memory_dir=tmp_path / "memory",
        reflector_strategy="sectioned",
        reflector_max_input_tokens=45000,
    )
    config.ensure_memory_dir()
    config.reflections_path.write_text(prior)
    config.observations_path.write_text(observations)

    def fake_compress(system_prompt, user_content, config, **kwargs):
        # Patch ONLY core-identity, preserving its existing bullet metadata id.
        return (
            "SECTION_HANDLE: ref:core-identity\n"
            "UPDATED_MARKDOWN:\n"
            "## Core Identity\n\n"
            "- **Name:** Alex Smith <!--om: id=ome_aaa kind=identity source_type=inferred confidence=medium "
            "sensitivity=personal actionability=high last_seen=2026-05-09T00:00:00Z seen_count=1 "
            "node=local scope=cluster-->\n"
        )

    monkeypatch.setattr(reflect, "compress", fake_compress)
    result = run_reflector(config, dry_run=True)
    assert result is not None

    # Touched section: fresh date; old marker dropped (exactly one marker).
    core = result.split("## Core Identity")[1].split("## ")[0]
    assert "last_reflected=2026-05-10" in core or "last_reflected=" in core
    assert "last_reflected=2026-05-09" not in core
    assert core.count("<!--om-section:") == 1
    # Untouched section keeps its honest PRIOR date.
    prefs = result.split("## Preferences & Opinions")[1]
    assert "last_reflected=2026-05-09 derived_from_obs_window=2026-05-08..2026-05-09" in prefs
    assert prefs.count("<!--om-section:") == 1


def test_section_provenance_self_heals_reflowed_stale_marker():
    """A model echo that reflows a stale `<!--om-section:` marker away from the
    heading (after a blank line) must NOT accumulate: a changed section drops
    EVERY stale marker in its body and emits exactly one fresh one."""
    from datetime import datetime, timezone

    from observational_memory.reflection_metadata import ensure_section_provenance

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    prior = (
        "## Core Identity\n\n"
        "<!--om-section: last_reflected=2026-05-30 derived_from_obs_window=2026-05-28..2026-05-29-->\n"
        "- Name: Alex\n"
    )
    # Model echoes the stale marker AFTER a blank line, then changes a bullet.
    reflowed = (
        "## Core Identity\n\n"
        "<!--om-section: last_reflected=2026-05-30 derived_from_obs_window=2026-05-28..2026-05-29-->\n\n"
        "- Name: Alex Smith\n"
    )
    out = ensure_section_provenance(reflowed, obs_window=("2026-05-31", "2026-05-31"), now=now, prior_text=prior)
    assert out.count("<!--om-section:") == 1
    assert "last_reflected=2026-05-30" not in out
    assert "last_reflected=2026-06-01" in out


def test_section_provenance_blank_line_before_external_marker_is_deduped():
    """A user/formatter that puts a blank line before an existing marker (Markdown
    is user-editable) must still converge to a single marker when the section is
    restamped — no two contradictory markers."""
    from datetime import datetime, timezone

    from observational_memory.reflection_metadata import ensure_section_provenance

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    doc = (
        "# Reflections\n\n## Core Identity\n\n"
        "<!--om-section: last_reflected=2026-05-01 derived_from_obs_window=2026-04-01..2026-05-01-->\n"
        "- Name: Alex\n"
    )
    # prior_text="" forces a restamp; the externally-positioned marker must be removed.
    out = ensure_section_provenance(doc, obs_window=("2026-05-28", "2026-06-01"), now=now)
    assert out.count("<!--om-section:") == 1
    assert "last_reflected=2026-05-01" not in out
