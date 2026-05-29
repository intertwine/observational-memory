"""Strict parser for the section-patch envelope the sectioned reflector emits.

Milestone 3 (section-targeted reflection, issue #71). In sectioned mode the
reflector does NOT rewrite the whole document. It returns one or more section
patches in a robust, line-oriented envelope:

    SECTION_HANDLE: ref:<section-slug>
    UPDATED_MARKDOWN:
    ## <Heading>
    ...the full replacement markdown for that one section...

    SECTION_HANDLE: ref:<other-slug>
    UPDATED_MARKDOWN:
    ## <Other Heading>
    ...

Each patch replaces exactly one existing section (or, with ``NEW_AFTER:``, adds
a new section after a named one). The reflector is told to emit ONLY sections it
actually changed; unchanged sections are reassembled byte-for-byte by
``reflection_sections.reassemble_document``.

SAFETY IS PARAMOUNT — this rewrites durable memory. The parser FAILS CLOSED: any
malformed envelope (missing markers, a handle with no markdown, a markdown block
that is not a single ``## `` section, a stray un-parseable region, or zero
patches) raises :class:`PatchParseError`. The caller leaves ``reflections.md``
untouched on any error. The parser NEVER silently drops or partially applies a
patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# An H2 header line (mirrors reflection_sections._H2_RE).
_H2_RE = re.compile(r"^## (?!#)(.+)$", re.MULTILINE)
# An H3 header line (mirrors reflection_sections._H3_RE), used for in-place
# subsection (``ref:<section>:<sub>``) patches whose body is a single ``### ``
# entry rather than a whole ``## `` section.
_H3_RE = re.compile(r"^### (?!#)(.+)$", re.MULTILINE)
# The envelope markers. Anchored at line start; the value is the rest of the
# SAME line ([^\n] so a blank value cannot swallow the next marker line).
_SECTION_HANDLE_RE = re.compile(r"^SECTION_HANDLE:[ \t]*([^\n]*?)[ \t]*$", re.MULTILINE)
_NEW_AFTER_RE = re.compile(r"^NEW_AFTER:[ \t]*([^\n]*?)[ \t]*$", re.MULTILINE)
_UPDATED_MARKDOWN_RE = re.compile(r"^UPDATED_MARKDOWN:\s*$", re.MULTILINE)


class PatchParseError(ValueError):
    """Raised when a section-patch envelope is malformed. Caller must fail closed."""


@dataclass(frozen=True)
class SectionPatch:
    """One parsed section patch.

    ``handle`` is the target section handle (``ref:<slug>``). ``markdown`` is the
    full replacement section text, validated to contain exactly one ``## ``
    header. ``new_after`` is set only for an addition: the handle of the existing
    section the new one should follow (``""`` means append at the end).
    """

    handle: str
    markdown: str
    new_after: str | None = None


# A patch begins at a SECTION_HANDLE line and runs until the next SECTION_HANDLE
# line or end of output. We split on the marker so trailing/leading whitespace
# between patches is tolerated.
_PATCH_SPLIT_RE = re.compile(r"^(?=SECTION_HANDLE:)", re.MULTILINE)


def parse_section_patches(raw: str) -> list[SectionPatch]:
    """Parse the reflector's section-patch envelope. Fail closed on anything off.

    Returns a non-empty list of :class:`SectionPatch`. Raises
    :class:`PatchParseError` for any malformed input so the caller can leave
    durable memory unchanged rather than write a partial/corrupt document.
    """
    if not raw or not raw.strip():
        raise PatchParseError("empty reflector output")

    text = raw.strip()
    if "SECTION_HANDLE:" not in text:
        raise PatchParseError("no SECTION_HANDLE marker found in reflector output")

    # Everything before the first SECTION_HANDLE must be blank (no stray prose
    # that we would otherwise silently discard).
    first = text.index("SECTION_HANDLE:")
    if text[:first].strip():
        raise PatchParseError("unexpected content before the first SECTION_HANDLE marker")

    raw_blocks = [block for block in _PATCH_SPLIT_RE.split(text[first:]) if block.strip()]
    if not raw_blocks:
        raise PatchParseError("no section patches found")

    # A line beginning with ``SECTION_HANDLE:`` inside a patch's UPDATED_MARKDOWN
    # body would otherwise split the patch in two (the envelope format is even
    # documented in reflections.md, so a reflection ABOUT this feature can contain
    # such a line). A genuine patch boundary always introduces its own
    # ``UPDATED_MARKDOWN:`` marker; a body line that merely starts with
    # ``SECTION_HANDLE:`` does not. So re-join any split fragment that lacks its
    # own ``UPDATED_MARKDOWN:`` marker back onto the preceding block — that text
    # belongs to the previous patch's markdown.
    blocks: list[str] = []
    for block in raw_blocks:
        if _UPDATED_MARKDOWN_RE.search(block) is None and blocks:
            blocks[-1] = blocks[-1] + block
        else:
            blocks.append(block)
    if not blocks:
        raise PatchParseError("no section patches found")

    patches: list[SectionPatch] = []
    seen_handles: set[str] = set()
    for block in blocks:
        patches.append(_parse_one(block))

    for patch in patches:
        # A handle may be patched once. (Additions use distinct new handles, so
        # this also guards against duplicate adds.)
        key = (patch.new_after is not None, patch.handle)
        if key in seen_handles:
            raise PatchParseError(f"duplicate patch for handle {patch.handle!r}")
        seen_handles.add(key)

    return patches


def _parse_one(block: str) -> SectionPatch:
    handle_match = _SECTION_HANDLE_RE.search(block)
    if handle_match is None:
        raise PatchParseError("section patch missing SECTION_HANDLE")
    handle = handle_match.group(1).strip()
    if not handle:
        raise PatchParseError("SECTION_HANDLE value is empty")

    md_marker = _UPDATED_MARKDOWN_RE.search(block)
    if md_marker is None:
        raise PatchParseError(f"section patch for {handle!r} missing UPDATED_MARKDOWN marker")

    # An optional NEW_AFTER marker must appear before UPDATED_MARKDOWN.
    new_after: str | None = None
    new_after_match = _NEW_AFTER_RE.search(block, 0, md_marker.start())
    if new_after_match is not None:
        # Empty value ("NEW_AFTER:") is valid and means "append at the end".
        new_after = new_after_match.group(1).strip()

    markdown = block[md_marker.end() :].strip("\n")
    if not markdown.strip():
        raise PatchParseError(f"section patch for {handle!r} has empty UPDATED_MARKDOWN")

    # An in-place subsection patch (handle ``ref:<section>:<sub>``, two colons)
    # carries a single ``### `` entry; everything else carries a single ``## ``
    # section. An addition (``new_after`` set) is always a new H2 section.
    is_subsection = new_after is None and handle.count(":") >= 2
    header_re = _H3_RE if is_subsection else _H2_RE
    level = "### " if is_subsection else "## "
    header_count = len(header_re.findall(markdown))
    if header_count != 1:
        raise PatchParseError(
            f"section patch for {handle!r} must contain exactly one '{level}' header (found {header_count})"
        )
    # The markdown must START with its header (no stray preamble that would be
    # silently merged into the previous section on reassembly).
    if not markdown.lstrip().startswith(level):
        raise PatchParseError(f"section patch for {handle!r} markdown must start with its '{level}' header")
    # A subsection patch must not smuggle a new ``## `` H2 header into the parent
    # section (which would split it). Reject any H2 in an H3 patch body.
    if is_subsection and _H2_RE.search(markdown):
        raise PatchParseError(f"subsection patch for {handle!r} must not contain a '## ' header")

    return SectionPatch(handle=handle, markdown=markdown, new_after=new_after)
