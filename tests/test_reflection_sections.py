"""Round-trip and substitution tests for reflection_sections (#71, Milestone 3).

This is the safety foundation for section-targeted reflection: it proves that
parse -> reassemble is byte-identical for a variety of real-shaped reflections.md
inputs (including the M2 scale fixtures) and that substituting one section changes
only that section. The reflector rewrites durable memory, so these tests exist to
guarantee that "do nothing" reassembly is a true no-op and that targeted patches
never silently drop, reorder, or mangle an unrelated section.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from observational_memory.reflection_metadata import ensure_section_provenance
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


# ---------------------------------------------------------------------------
# In-place H3 subsection replacement (the project-update path).
# ---------------------------------------------------------------------------


def test_subsection_replacement_updates_one_h3_and_preserves_siblings() -> None:
    # Updating one project H3 in place must leave its sibling H3 entries AND every
    # other section byte-for-byte. This is the in-place project-update path that
    # makes "an observation about one project updates only that subsection" real.
    parsed = parse_reflection_document(_FULL)
    new_sub = "### observational-memory\n\n- Section-targeted reflection SHIPPED.\n"
    result = reassemble_document(
        parsed,
        subsection_replacements={"ref:active-projects:observational-memory": new_sub},
    )
    reparsed = parse_reflection_document(result)
    active = reparsed.section_by_handle("ref:active-projects")
    assert active is not None
    assert "SHIPPED." in active.text
    # The sibling project H3 survived verbatim.
    assert "Porting auth flows from upstream." in active.text
    # Every section OTHER than Active Projects is byte-identical.
    for original in parsed.sections:
        if original.handle == "ref:active-projects":
            continue
        rebuilt = reparsed.section_by_handle(original.handle)
        assert rebuilt is not None
        assert rebuilt.text == original.text


def test_subsection_replacement_last_h3_keeps_section_shape() -> None:
    # Replacing the LAST H3 in a section must not introduce a trailing blank-line
    # change that would corrupt the byte shape of following sections.
    parsed = parse_reflection_document(_FULL)
    new_sub = "### hermes-agent\n\n- Auth port complete.\n"
    result = reassemble_document(
        parsed,
        subsection_replacements={"ref:active-projects:hermes-agent": new_sub},
    )
    reparsed = parse_reflection_document(result)
    assert "Auth port complete." in reparsed.section_by_handle("ref:active-projects").text
    # The first H3 sibling is untouched.
    assert "Reflector scaling work, issue #71." in reparsed.section_by_handle("ref:active-projects").text
    # Sections after Active Projects are byte-identical (no jammed header).
    for original in parsed.sections:
        if original.handle == "ref:active-projects":
            continue
        assert reparsed.section_by_handle(original.handle).text == original.text


def test_unknown_subsection_handle_fails_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(KeyError):
        reassemble_document(parsed, subsection_replacements={"ref:active-projects:nope": "### nope\n\n- x\n"})


def test_subsection_replacement_must_be_single_h3() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(
            parsed,
            subsection_replacements={"ref:active-projects:observational-memory": "## Wrong Level\n\n- x\n"},
        )


def test_subsection_and_parent_cannot_both_be_patched() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(
            parsed,
            replacements={"ref:active-projects": "## Active Projects\n\n### observational-memory\n\n- a\n"},
            subsection_replacements={"ref:active-projects:observational-memory": "### observational-memory\n\n- b\n"},
        )


# ---------------------------------------------------------------------------
# Addition safety: no duplicate H2, no jammed header against prior content.
# ---------------------------------------------------------------------------


def test_addition_with_colliding_heading_fails_closed() -> None:
    # A NEW_AFTER whose heading slug collides with an existing section must be
    # rejected — otherwise the document grows a second "## Active Projects" and
    # the core bundle / materializer see a duplicate, conflicting section.
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(
            parsed,
            additions=[("ref:core-identity", "## Active Projects\n\n### dup\n\n- x\n")],
        )


def test_two_additions_with_same_heading_fail_closed() -> None:
    parsed = parse_reflection_document(_FULL)
    with pytest.raises(ValueError):
        reassemble_document(
            parsed,
            additions=[
                ("", "## Brand New\n\n- one\n"),
                ("", "## Brand New\n\n- two\n"),
            ],
        )


def test_addition_after_single_newline_section_is_separated_by_blank_line() -> None:
    # When the anchor section's text ends with a single "\n" (the normal case for
    # the document's last section), a NEW_AFTER insertion must still leave a blank
    # line between the previous content and the new "## " header — never "alpha\n##
    # New". Build a doc whose last section ends with exactly one newline.
    doc = "# Reflections\n\n## Core Identity\n\n- Alex\n"
    parsed = parse_reflection_document(doc)
    result = reassemble_document(parsed, additions=[("ref:core-identity", "## New Area\n\n- fresh\n")])
    assert "- Alex\n\n## New Area" in result
    assert "- Alex\n## New Area" not in result
    # And it still parses into two clean sections.
    reparsed = parse_reflection_document(result)
    assert reparsed.handles() == ["ref:core-identity", "ref:new-area"]


def test_end_addition_after_single_newline_section_is_separated() -> None:
    doc = "# Reflections\n\n## Core Identity\n\n- Alex\n"
    parsed = parse_reflection_document(doc)
    result = reassemble_document(parsed, additions=[("", "## Appended\n\n- end\n")])
    assert "- Alex\n\n## Appended" in result
    assert "- Alex\n## Appended" not in result


# ---------------------------------------------------------------------------
# Gate 3: a section-level provenance stamp must not break byte-faithful
# reassembly or perturb section/subsection handles.
# ---------------------------------------------------------------------------

_GATE3_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_round_trip_byte_identical_with_section_stamp() -> None:
    """A stamped document still parses and reassembles byte-for-byte; the
    `<!--om-section:` line never creates an H2 boundary and rides reassembly."""
    stamped = ensure_section_provenance(_FULL, obs_window=("2026-04-28", "2026-05-01"), now=_GATE3_NOW)
    parsed = parse_reflection_document(stamped)
    assert parsed.render() == stamped
    assert reassemble_document(parsed) == stamped
    # The stamp did not invent extra sections.
    assert len(parsed.sections) == len(parse_reflection_document(_FULL).sections)


def test_section_handles_unchanged_by_stamp() -> None:
    """Slugs/handles for a stamped doc equal those for the unstamped doc."""
    unstamped = parse_reflection_document(_FULL)
    stamped = parse_reflection_document(
        ensure_section_provenance(_FULL, obs_window=("2026-04-28", "2026-05-01"), now=_GATE3_NOW)
    )
    assert stamped.handles() == unstamped.handles()
    # Subsection handles are also unaffected (stamp is H2-only).
    active_before = unstamped.section_by_handle("ref:active-projects")
    active_after = stamped.section_by_handle("ref:active-projects")
    assert active_before is not None and active_after is not None
    assert [s.handle for s in active_after.subsections] == [s.handle for s in active_before.subsections]
