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


def reassemble_document(
    document: ReflectionDocument,
    *,
    replacements: dict[str, str] | None = None,
    additions: list[tuple[str, str]] | None = None,
) -> str:
    """Deterministically reassemble ``reflections.md`` from parsed sections.

    Args:
        document: The parsed source document.
        replacements: Map of existing section handle -> replacement markdown
            (the full section including its ``## `` header). Every handle must
            exist in ``document`` or the call fails closed with ``KeyError`` so a
            stale/hallucinated handle never silently drops a section.
        additions: Ordered ``(after_handle, markdown)`` pairs appending a new
            section immediately after the named existing section. ``after_handle``
            may be ``""`` to append at the very end. The markdown must contain a
            single H2 header; a malformed addition fails closed with ``ValueError``.

    Returns:
        The reassembled document. Unchanged sections are emitted byte-for-byte,
        original order is preserved, and the prelude is untouched.

    Safety: this never partially writes. Any invalid handle or malformed section
    raises before any string is returned, so the caller can leave the on-disk
    ``reflections.md`` unchanged.
    """
    replacements = replacements or {}
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

    # Validate additions up front (fail closed before emitting anything).
    valid_after = known | {""}
    appended_by_anchor: dict[str, list[str]] = {}
    end_appends: list[str] = []
    for after_handle, markdown in additions:
        if after_handle not in valid_after:
            raise KeyError(f"reflection_sections: unknown addition anchor handle: {after_handle!r}")
        if len(_H2_RE.findall(markdown)) != 1:
            raise ValueError("reflection_sections: each added section must contain exactly one H2 header")
        normalized = _normalize_section_text(markdown)
        if after_handle == "":
            end_appends.append(normalized)
        else:
            appended_by_anchor.setdefault(after_handle, []).append(normalized)

    parts: list[str] = [document.prelude]
    for section in document.sections:
        if section.handle in replacements:
            parts.append(_normalize_section_text(replacements[section.handle]))
        else:
            parts.append(section.text)
        for added in appended_by_anchor.get(section.handle, []):
            parts.append(added)

    parts.extend(end_appends)

    return "".join(parts)
