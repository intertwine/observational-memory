"""Round-trip and substitution tests for reflection_sections (#71, Milestone 3).

This is the safety foundation for section-targeted reflection: it proves that
parse -> reassemble is byte-identical for a variety of real-shaped reflections.md
inputs (including the M2 scale fixtures) and that substituting one section changes
only that section. The reflector rewrites durable memory, so these tests exist to
guarantee that "do nothing" reassembly is a true no-op and that targeted patches
never silently drop, reorder, or mangle an unrelated section.
"""

from __future__ import annotations

import pytest

from observational_memory.reflection_sections import (
    parse_reflection_document,
    reassemble_document,
    slugify,
)

from ._scale_fixtures import SCENARIOS, make_reflections, make_scenario

# ---------------------------------------------------------------------------
# Sample documents (real-shaped, hand-written) for explicit-shape coverage.
# ---------------------------------------------------------------------------

_FULL = """# Reflections

*Last updated: 2026-05-01 09:00 UTC*
*Last reflected: 2026-05-01*

## Core Identity

- Bryan builds Observational Memory.
- Prefers uv and ruff.

## Active Projects

### observational-memory

- Reflector scaling work, issue #71.
- Section-targeted reflection is the bridge.

### hermes-agent

- Porting auth flows from upstream.

## Preferences & Opinions

- Conversational, narrative reports.

## Key Facts & Context

- Email: bryan@example.com.

## Archive

### old-project

- Completed last quarter.
"""

_WITH_METADATA = """# Reflections

*Last updated: 2026-05-01 09:00 UTC*
*Last reflected: 2026-05-01*
<!--om: doc=reflections version=1-->

## Core Identity

- A durable fact. <!--om: last_seen=2026-05-01-->

## Key Facts & Context

- Another fact.
"""

_NO_H3 = """# Reflections

*Last updated: 2026-05-01 09:00 UTC*

## Core Identity

- Just a flat section.

## Key Facts & Context

- No subsections here either.
"""

_TRAILING_WS = (
    "# Reflections\n\n## Core Identity\n\n- Fact one.   \n- Fact two.\t\n\n\n## Key Facts & Context\n\n- Fact.\n   \n"
)

_DUPLICATE_HEADINGS = """# Reflections

## Notes

- First notes block.

## Notes

- Second notes block, same heading text.

### Detail

- A subsection.

### Detail

- A duplicate-looking subsection heading.
"""

_NO_PRELUDE = "## Core Identity\n\n- Starts immediately with an H2.\n\n## Key Facts\n\n- No title prelude.\n"

_EMPTY = ""

_PRELUDE_ONLY = "# Reflections\n\n*Last updated: 2026-05-01*\n\nNo sections yet.\n"

_ALL_SAMPLES = {
    "full": _FULL,
    "with_metadata": _WITH_METADATA,
    "no_h3": _NO_H3,
    "trailing_ws": _TRAILING_WS,
    "duplicate_headings": _DUPLICATE_HEADINGS,
    "no_prelude": _NO_PRELUDE,
    "empty": _EMPTY,
    "prelude_only": _PRELUDE_ONLY,
}


# ---------------------------------------------------------------------------
# Round-trip: parse -> render is byte-identical.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_ALL_SAMPLES))
def test_round_trip_is_byte_identical(name: str) -> None:
    content = _ALL_SAMPLES[name]
    parsed = parse_reflection_document(content)
    assert parsed.render() == content


@pytest.mark.parametrize("name", sorted(_ALL_SAMPLES))
def test_reassemble_no_changes_is_byte_identical(name: str) -> None:
    """Reassembly with no replacements/additions must be a true no-op."""
    content = _ALL_SAMPLES[name]
    parsed = parse_reflection_document(content)
    assert reassemble_document(parsed) == content


@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS))
def test_round_trip_m2_scale_fixtures(scenario_name: str) -> None:
    reflections, _observations = make_scenario(SCENARIOS[scenario_name])
    parsed = parse_reflection_document(reflections)
    assert parsed.render() == reflections
    assert reassemble_document(parsed) == reflections


def test_round_trip_various_target_sizes() -> None:
    for target in (8_000, 48_000, 96_000, 200_000):
        reflections = make_reflections(target)
        parsed = parse_reflection_document(reflections)
        assert parsed.render() == reflections


# ---------------------------------------------------------------------------
# Parse structure: prelude, sections, slugs, handles, order, subsections.
# ---------------------------------------------------------------------------


def test_prelude_holds_title_and_timestamps() -> None:
    parsed = parse_reflection_document(_FULL)
    assert parsed.prelude.startswith("# Reflections\n")
    assert "*Last updated: 2026-05-01 09:00 UTC*" in parsed.prelude
    assert "*Last reflected: 2026-05-01*" in parsed.prelude
    # The prelude stops at the first H2 header.
    assert "## Core Identity" not in parsed.prelude


def test_section_handles_align_with_recall_slugs() -> None:
    parsed = parse_reflection_document(_FULL)
    handles = parsed.handles()
    assert handles == [
        "ref:core-identity",
        "ref:active-projects",
        "ref:preferences-opinions",
        "ref:key-facts-context",
        "ref:archive",
    ]


def test_subsection_handles_use_parent_slug() -> None:
    parsed = parse_reflection_document(_FULL)
    active = parsed.section_by_handle("ref:active-projects")
    assert active is not None
    sub_handles = [sub.handle for sub in active.subsections]
    assert sub_handles == [
        "ref:active-projects:observational-memory",
        "ref:active-projects:hermes-agent",
    ]


def test_slug_matches_parser_transform() -> None:
    assert slugify("Core Identity") == "core-identity"
    assert slugify("Preferences & Opinions") == "preferences-opinions"
    assert slugify("Key Facts & Context") == "key-facts-context"
    # Degenerate heading still yields a stable, non-empty slug.
    assert slugify("###") == "section"


def test_original_order_preserved() -> None:
    parsed = parse_reflection_document(_FULL)
    orders = [s.order for s in parsed.sections]
    assert orders == sorted(orders)
    assert orders == list(range(len(parsed.sections)))


def test_no_h3_section_has_no_subsections() -> None:
    parsed = parse_reflection_document(_NO_H3)
    for section in parsed.sections:
        assert section.subsections == ()


def test_empty_document_has_no_sections() -> None:
    parsed = parse_reflection_document(_EMPTY)
    assert parsed.sections == ()
    assert parsed.prelude == ""
    assert parsed.render() == ""


def test_prelude_only_document_keeps_everything_in_prelude() -> None:
    parsed = parse_reflection_document(_PRELUDE_ONLY)
    assert parsed.sections == ()
    assert parsed.prelude == _PRELUDE_ONLY


def test_no_prelude_document_has_empty_prelude() -> None:
    parsed = parse_reflection_document(_NO_PRELUDE)
    assert parsed.prelude == ""
    assert parsed.sections[0].handle == "ref:core-identity"


def test_duplicate_headings_get_unique_handles() -> None:
    parsed = parse_reflection_document(_DUPLICATE_HEADINGS)
    handles = parsed.handles()
    assert handles == ["ref:notes", "ref:notes-2"]
    # Duplicate H3 headings inside the second section also disambiguate.
    second = parsed.section_by_handle("ref:notes-2")
    assert second is not None
    sub_handles = [sub.handle for sub in second.subsections]
    assert sub_handles == ["ref:notes-2:detail", "ref:notes-2:detail-2"]


# ---------------------------------------------------------------------------
# Substitution: changing one section changes ONLY that section.
# ---------------------------------------------------------------------------


def test_substitute_one_section_changes_only_that_section() -> None:
    parsed = parse_reflection_document(_FULL)
    new_archive = "## Archive\n\n### old-project\n\n- Completed and now annotated.\n"
    result = reassemble_document(parsed, replacements={"ref:archive": new_archive})

    reparsed = parse_reflection_document(result)
    # Every section other than Archive is byte-identical to the original.
    for original in parsed.sections:
        rebuilt = reparsed.section_by_handle(original.handle)
        assert rebuilt is not None
        if original.handle == "ref:archive":
            assert "now annotated" in rebuilt.text
        else:
            assert rebuilt.text == original.text
    # Prelude untouched.
    assert reparsed.prelude == parsed.prelude


def test_substitution_preserves_prelude_and_metadata() -> None:
    parsed = parse_reflection_document(_WITH_METADATA)
    new_core = "## Core Identity\n\n- Updated durable fact.\n"
    result = reassemble_document(parsed, replacements={"ref:core-identity": new_core})
    assert "<!--om: doc=reflections version=1-->" in result
    assert "*Last updated: 2026-05-01 09:00 UTC*" in result
    assert "Updated durable fact." in result
    # The Key Facts section (and its body) is byte-preserved.
    assert "## Key Facts & Context\n\n- Another fact.\n" in result


def test_substitution_keeps_section_order() -> None:
    parsed = parse_reflection_document(_FULL)
    result = reassemble_document(
        parsed,
        replacements={"ref:core-identity": "## Core Identity\n\n- Reordered? No.\n"},
    )
    reparsed = parse_reflection_document(result)
    assert reparsed.handles() == parsed.handles()


def test_unknown_replacement_handle_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(KeyError):
        reassemble_document(parsed, replacements={"ref:does-not-exist": "## Nope\n\n- x\n"})


def test_headerless_replacement_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(parsed, replacements={"ref:archive": "- no header here\n"})


def test_replacement_with_two_headers_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(
            parsed,
            replacements={"ref:archive": "## Archive\n\n- x\n\n## Smuggled\n\n- y\n"},
        )


# ---------------------------------------------------------------------------
# Additions: appending a new section in the right place.
# ---------------------------------------------------------------------------


def test_add_section_after_anchor() -> None:
    parsed = parse_reflection_document(_FULL)
    new_section = "## Recent Themes\n\n- Section-targeted reflection.\n"
    result = reassemble_document(
        parsed,
        additions=[("ref:active-projects", new_section)],
    )
    reparsed = parse_reflection_document(result)
    assert reparsed.handles() == [
        "ref:core-identity",
        "ref:active-projects",
        "ref:recent-themes",
        "ref:preferences-opinions",
        "ref:key-facts-context",
        "ref:archive",
    ]
    # Existing sections other than the inserted one are byte-preserved.
    for original in parsed.sections:
        rebuilt = reparsed.section_by_handle(original.handle)
        assert rebuilt is not None
        assert rebuilt.text == original.text


def test_add_section_at_end() -> None:
    parsed = parse_reflection_document(_FULL)
    result = reassemble_document(
        parsed,
        additions=[("", "## Brand New\n\n- Appended at the end.\n")],
    )
    reparsed = parse_reflection_document(result)
    assert reparsed.handles()[-1] == "ref:brand-new"
    assert reparsed.prelude == parsed.prelude


def test_unknown_addition_anchor_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(KeyError):
        reassemble_document(parsed, additions=[("ref:nope", "## New\n\n- x\n")])


def test_headerless_addition_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(parsed, additions=[("", "- no header\n")])


def test_combined_replacement_and_addition() -> None:
    parsed = parse_reflection_document(_FULL)
    result = reassemble_document(
        parsed,
        replacements={"ref:core-identity": "## Core Identity\n\n- Updated.\n"},
        additions=[("ref:core-identity", "## New After Core\n\n- Inserted.\n")],
    )
    reparsed = parse_reflection_document(result)
    assert reparsed.handles() == [
        "ref:core-identity",
        "ref:new-after-core",
        "ref:active-projects",
        "ref:preferences-opinions",
        "ref:key-facts-context",
        "ref:archive",
    ]
    assert "Updated." in reparsed.section_by_handle("ref:core-identity").text


def test_substitution_round_trips_on_scale_fixture() -> None:
    """Substituting one project on a 10x fixture leaves all other sections intact."""
    reflections, _ = make_scenario(SCENARIOS["10x"])
    parsed = parse_reflection_document(reflections)
    active = parsed.section_by_handle("ref:active-projects")
    assert active is not None
    new_active = active.text.rstrip("\n") + "\n\n- An appended durable note.\n\n"
    result = reassemble_document(parsed, replacements={"ref:active-projects": new_active})
    reparsed = parse_reflection_document(result)
    for original in parsed.sections:
        rebuilt = reparsed.section_by_handle(original.handle)
        assert rebuilt is not None
        if original.handle != "ref:active-projects":
            assert rebuilt.text == original.text
    assert "An appended durable note." in result
