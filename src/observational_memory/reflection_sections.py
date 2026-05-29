"""Parse and deterministically reassemble ``reflections.md`` by section.

This is the foundation for Milestone 3 (section-targeted reflection, issue #71).
Instead of asking the LLM to rewrite the entire ``reflections.md`` document on
every fold, a section-targeted reflector patches only the sections an observation
chunk actually touches and then deterministically reassembles the full document.

The hard safety requirement is that reassembly is **byte-faithful**: any section
that is not deliberately substituted must come back out exactly as it went in,
the original section order is preserved, and the title/timestamp prelude plus any
OM metadata comments survive untouched. Invalid input must fail closed — callers
that hand us a malformed substitution get an exception and can leave durable
memory unchanged rather than write a partial or corrupt document.

Slug scheme (aligned with ``search/parser.py`` and ``startup_memory`` handles):

    ref:<section-slug>                      H2 section handle
    ref:<section-slug>:<subsection-slug>     H3 subsection handle

where the slug is ``re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")`` — the
same transform ``parse_reflections`` and ``startup_memory._slug`` use, so a
section handle here is the same string a startup recall handle uses after its
``startup:``/``ref:`` prefix. Duplicate-looking headings get a ``-2``, ``-3`` …
suffix so every handle stays unique and stable for a given document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# An H2 header line: "## " then the heading text. We anchor at the start of a
# line (MULTILINE) and require exactly two leading hashes followed by a space so
# we do not split on H3 ("### ") headers.
_H2_RE = re.compile(r"^## (?!#)(.+)$", re.MULTILINE)
# An H3 header line, used only to enumerate subsections for slug/handle purposes.
# The section's raw text is preserved regardless of how H3s are detected.
_H3_RE = re.compile(r"^### (?!#)(.+)$", re.MULTILINE)


def slugify(heading: str) -> str:
    """Slugify a heading the same way the search parser and startup handles do."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


@dataclass(frozen=True)
class Subsection:
    """An H3 subsection within an H2 section (handle/slug metadata only).

    ``text`` is the raw slice of the parent section from this H3 header up to the
    next H3 header (or the end of the section). It is informational: reassembly
    operates at the section level, so subsection slices are for routing and
    diagnostics, not for byte-level reassembly.
    """

    heading: str
    slug: str
    handle: str
    text: str


@dataclass(frozen=True)
class Section:
    """A top-level (H2) section of ``reflections.md``.

    ``text`` is the section's exact bytes — from (and including) its ``## ``
    header line up to (but not including) the next H2 header, with trailing
    newlines preserved exactly as they appeared in the source. Reassembly joins
    section ``text`` values back-to-back, so preserving these bytes verbatim is
    what makes the round trip byte-identical.
    """

    heading: str
    slug: str
    handle: str
    text: str
    order: int
    subsections: tuple[Subsection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReflectionDocument:
    """A parsed ``reflections.md`` split into prelude + ordered H2 sections.

    ``prelude`` is everything before the first H2 header (the ``# Reflections``
    title, the ``*Last updated*`` / ``*Last reflected*`` lines, any leading OM
    metadata comments, and the blank lines between them) preserved verbatim. If
    the document has no H2 headers the entire document is the prelude.

    ``prelude + "".join(s.text for s in sections)`` reconstructs the original
    document byte-for-byte.
    """

    prelude: str
    sections: tuple[Section, ...]

    def section_by_handle(self, handle: str) -> Section | None:
        for section in self.sections:
            if section.handle == handle:
                return section
        return None

    def handles(self) -> list[str]:
        return [section.handle for section in self.sections]

    def render(self) -> str:
        """Reassemble the full document byte-for-byte from prelude + sections."""
        return self.prelude + "".join(section.text for section in self.sections)


def _parse_subsections(section_text: str, section_slug: str) -> tuple[Subsection, ...]:
    """Enumerate H3 subsections inside a section's raw text.

    Slugs are made unique within the section so duplicate-looking H3 headings get
    stable ``-2``/``-3`` suffixes. The first H3 may begin partway through the
    section text; everything before it (the H2 header line and any section
    preamble) is not a subsection and is intentionally not represented here.
    """
    matches = list(_H3_RE.finditer(section_text))
    if not matches:
        return ()

    seen: dict[str, int] = {}
    subsections: list[Subsection] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section_text)
        heading = match.group(1).strip()
        base_slug = slugify(heading)
        count = seen.get(base_slug, 0) + 1
        seen[base_slug] = count
        slug = base_slug if count == 1 else f"{base_slug}-{count}"
        subsections.append(
            Subsection(
                heading=heading,
                slug=slug,
                handle=f"ref:{section_slug}:{slug}",
                text=section_text[start:end],
            )
        )
    return tuple(subsections)


def parse_reflection_document(content: str) -> ReflectionDocument:
    """Parse ``reflections.md`` content into a byte-faithful section document.

    The parse is lossless: the prelude plus every section's raw ``text`` slice
    concatenate back to ``content`` exactly. Section order matches the source.
    Duplicate-looking H2 headings receive stable ``-2``/``-3`` slug suffixes so
    every handle is unique.
    """
    matches = list(_H2_RE.finditer(content))
    if not matches:
        return ReflectionDocument(prelude=content, sections=())

    prelude = content[: matches[0].start()]

    seen: dict[str, int] = {}
    sections: list[Section] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        text = content[start:end]
        heading = match.group(1).strip()
        base_slug = slugify(heading)
        count = seen.get(base_slug, 0) + 1
        seen[base_slug] = count
        slug = base_slug if count == 1 else f"{base_slug}-{count}"
        handle = f"ref:{slug}"
        sections.append(
            Section(
                heading=heading,
                slug=slug,
                handle=handle,
                text=text,
                order=index,
                subsections=_parse_subsections(text, slug),
            )
        )

    return ReflectionDocument(prelude=prelude, sections=tuple(sections))


def _normalize_section_text(text: str) -> str:
    """Ensure a substituted/new section's text ends with exactly one blank line.

    Sections in a well-formed document are separated by a blank line. A caller's
    replacement markdown may or may not include its own trailing newlines; we
    normalize to a single trailing ``\\n\\n`` so reassembly stays well-formed and
    appending later sections does not jam two headers together. This only touches
    text the caller is *deliberately* substituting — unchanged sections are never
    normalized, so the unchanged-byte guarantee holds.
    """
    return text.rstrip("\n") + "\n\n"


def _ensure_trailing_blank_line(text: str) -> str:
    """Ensure *text* ends with a blank line so an inserted header is separated.

    Used to normalize the PRECEDING section's text right before a NEW_AFTER
    insertion: when an anchor section's slice ends with a single ``\\n`` (the
    normal case for the document's last section, or any section parsed up to
    EOF), inserting a header straight after it would jam ``- last bullet\\n##
    New`` together with no blank line. We only apply this to the boundary
    immediately before an inserted section — sections with nothing inserted after
    them are emitted byte-for-byte and never touched.
    """
    if not text:
        return text
    return text.rstrip("\n") + "\n\n"


def _replace_subsections_in_section(section: Section, replacements: dict[str, str]) -> str:
    """Return *section*'s text with one or more H3 subsections replaced in place.

    The parent H2 header, the section preamble, and every SIBLING H3 entry are
    preserved byte-for-byte; only the slices of the named H3 entries (each from
    its ``### `` header to the next H3 / end of section) are substituted. This is
    the in-place project-update path: an observation about one project updates
    ONLY that project's H3 entry without re-sending or risking its siblings.

    Substitutions are applied from the LAST H3 to the FIRST so earlier slice
    offsets stay valid as later ones are spliced in.

    Fails closed: raises ``KeyError`` if a handle is not an H3 of this section,
    and ``ValueError`` if a replacement is not a single ``### `` subsection.
    """
    handle_to_index = {sub.handle: i for i, sub in enumerate(section.subsections)}
    for sub_handle, markdown in replacements.items():
        if sub_handle not in handle_to_index:
            raise KeyError(f"reflection_sections: unknown subsection handle: {sub_handle!r}")
        if len(_H3_RE.findall(markdown)) != 1:
            raise ValueError(
                f"reflection_sections: subsection replacement for {sub_handle!r} must contain exactly one H3 header"
            )
        if not markdown.lstrip().startswith("### "):
            raise ValueError(
                f"reflection_sections: subsection replacement for {sub_handle!r} must start with its '### ' header"
            )

    text = section.text
    matches = list(_H3_RE.finditer(text))
    body_end = len(text.rstrip("\n"))
    # Apply in descending index order so splicing one slice never shifts the
    # offsets of an unprocessed earlier slice.
    for sub_handle in sorted(replacements, key=lambda h: handle_to_index[h], reverse=True):
        index = handle_to_index[sub_handle]
        start = matches[index].start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        tail = text[end:]
        is_last_entry = index + 1 >= len(matches) and end >= body_end
        # One trailing blank line keeps the next sibling H3 separated; the final
        # H3 keeps the section's single-trailing-newline shape instead of adding
        # a blank line at the very end of the section body.
        if is_last_entry and not tail.strip():
            new_sub = replacements[sub_handle].rstrip("\n") + "\n"
        else:
            new_sub = replacements[sub_handle].rstrip("\n") + "\n\n"
        text = text[:start] + new_sub + tail
    return text


def reassemble_document(
    document: ReflectionDocument,
    *,
    replacements: dict[str, str] | None = None,
    subsection_replacements: dict[str, str] | None = None,
    additions: list[tuple[str, str]] | None = None,
) -> str:
    """Deterministically reassemble ``reflections.md`` from parsed sections.

    Args:
        document: The parsed source document.
        replacements: Map of existing H2 section handle -> replacement markdown
            (the full section including its ``## `` header). Every handle must
            exist in ``document`` or the call fails closed with ``KeyError`` so a
            stale/hallucinated handle never silently drops a section.
        subsection_replacements: Map of existing H3 subsection handle
            (``ref:<section>:<sub>``) -> replacement markdown (a single ``### ``
            subsection). The parent H2 header, preamble, and every sibling H3 are
            preserved byte-for-byte; only the named H3's slice is substituted.
            This is the in-place project-update path. A handle whose parent H2 is
            also in ``replacements`` is rejected (the two would fight over the
            same section). Unknown/malformed handles fail closed.
        additions: Ordered ``(after_handle, markdown)`` pairs appending a new
            section immediately after the named existing section. ``after_handle``
            may be ``""`` to append at the very end. The markdown must contain a
            single H2 header whose slug does NOT collide with an existing section
            (or an earlier addition); a malformed/colliding addition fails closed
            with ``ValueError``.

    Returns:
        The reassembled document. Unchanged sections are emitted byte-for-byte,
        original order is preserved, and the prelude is untouched.

    Safety: this never partially writes. Any invalid handle or malformed section
    raises before any string is returned, so the caller can leave the on-disk
    ``reflections.md`` unchanged.
    """
    replacements = replacements or {}
    subsection_replacements = subsection_replacements or {}
    additions = additions or []

    known = set(document.handles())
    unknown = [handle for handle in replacements if handle not in known]
    if unknown:
        raise KeyError(f"reflection_sections: unknown replacement handle(s): {sorted(unknown)}")

    # Validate that each replacement still carries exactly one H2 header so we
    # never substitute a header-less blob that would silently merge into a
    # neighbor or lose the section heading.
    for handle, markdown in replacements.items():
        if len(_H2_RE.findall(markdown)) != 1:
            raise ValueError(f"reflection_sections: replacement for {handle!r} must contain exactly one H2 header")

    # Resolve subsection replacements to their parent section, validating up front.
    sub_parent_handle: dict[str, str] = {}
    for section in document.sections:
        for sub in section.subsections:
            sub_parent_handle[sub.handle] = section.handle
    subs_by_parent: dict[str, dict[str, str]] = {}
    for sub_handle, markdown in subsection_replacements.items():
        parent_handle = sub_parent_handle.get(sub_handle)
        if parent_handle is None:
            raise KeyError(f"reflection_sections: unknown subsection handle: {sub_handle!r}")
        if parent_handle in replacements:
            raise ValueError(
                f"reflection_sections: subsection {sub_handle!r} and its parent {parent_handle!r} "
                "cannot both be patched in the same fold"
            )
        subs_by_parent.setdefault(parent_handle, {})[sub_handle] = markdown

    # Existing section slugs an addition's heading must not collide with — a
    # NEW_AFTER must create a genuinely new section, never a duplicate H2.
    existing_slugs = {section.slug for section in document.sections}

    # Validate additions up front (fail closed before emitting anything).
    valid_after = known | {""}
    appended_by_anchor: dict[str, list[str]] = {}
    end_appends: list[str] = []
    for after_handle, markdown in additions:
        if after_handle not in valid_after:
            raise KeyError(f"reflection_sections: unknown addition anchor handle: {after_handle!r}")
        headings = _H2_RE.findall(markdown)
        if len(headings) != 1:
            raise ValueError("reflection_sections: each added section must contain exactly one H2 header")
        new_slug = slugify(headings[0].strip())
        if new_slug in existing_slugs:
            raise ValueError(
                f"reflection_sections: added section heading collides with existing section slug {new_slug!r}; "
                "update the existing section instead of adding a duplicate"
            )
        existing_slugs.add(new_slug)  # also catch collisions between two additions
        normalized = _normalize_section_text(markdown)
        if after_handle == "":
            end_appends.append(normalized)
        else:
            appended_by_anchor.setdefault(after_handle, []).append(normalized)

    parts: list[str] = [document.prelude]
    for section in document.sections:
        has_addition = bool(appended_by_anchor.get(section.handle))
        if section.handle in replacements:
            section_text = _normalize_section_text(replacements[section.handle])
        elif section.handle in subs_by_parent:
            section_text = _replace_subsections_in_section(section, subs_by_parent[section.handle])
        else:
            section_text = section.text
        # If a new section will be inserted right after this one, make sure this
        # section's text ends with a blank line so the inserted ``## `` header is
        # not jammed onto the previous content line.
        if has_addition:
            section_text = _ensure_trailing_blank_line(section_text)
        parts.append(section_text)
        for added in appended_by_anchor.get(section.handle, []):
            parts.append(added)

    # An addition appended at the very end must also be separated from the
    # document's last section by a blank line.
    if end_appends and parts:
        parts[-1] = _ensure_trailing_blank_line(parts[-1])
    parts.extend(end_appends)

    return "".join(parts)
