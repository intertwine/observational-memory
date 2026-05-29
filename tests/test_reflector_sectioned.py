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
