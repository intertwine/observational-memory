"""Deterministic routing from observation chunks to impacted reflection sections.

Milestone 3 (section-targeted reflection, issue #71). Instead of re-sending the
whole ``reflections.md`` on every fold, a section-targeted reflector decides —
with cheap, deterministic heuristics and no extra LLM call — which sections an
observation chunk actually touches, then sends only those (plus an always-visible
core bundle) to the model.

Targeting works at two granularities, because real reflections.md is dominated by
a FEW big H2 sections (``Active Projects``, ``Archive``) that hold MANY modest H3
entries:

  - **Section granularity** for the small durable sections (the core bundle, and
    small sections like ``Recent Themes``): the whole H2 section rides along.
  - **Subsection granularity** for the heavy H2 sections: only the matching H3
    entry (one project / one archived item) is surfaced, NOT the whole H2. This
    is what keeps the per-fold context proportional to the touched work rather
    than to the document size.

The two jobs here:

  1. ``CORE_BUNDLE_HEADINGS`` — the durable sections that ride along in EVERY
     fold so the model never loses identity/preferences/relationship/key-facts
     while patching one project, plus ``Recent Themes`` when the update concerns
     current work and the matching project H3 entry when one is detected.
  2. ``route_chunk`` — map a single observation chunk to a :class:`RouteResult`:
     the full-section handles to surface in their entirety plus the subsection
     handles to surface on their own. Routing is deterministic and total: if
     nothing matches, it falls back to a stable rotation across the H3 entries so
     that, across folds, coverage reaches PAST the document head (legacy
     head-only truncation never does), which is the whole point of the milestone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .reflection_sections import ReflectionDocument, Section, Subsection, slugify

# Durable sections that must ride along in EVERY fold's context regardless of
# which observation chunk is being folded.
CORE_BUNDLE_HEADINGS: tuple[str, ...] = (
    "Core Identity",
    "Preferences & Opinions",
    "Relationship & Communication",
    "Key Facts & Context",
)

ACTIVE_PROJECTS_HEADING = "Active Projects"
RECENT_THEMES_HEADING = "Recent Themes"

_CORE_SLUGS: frozenset[str] = frozenset(slugify(h) for h in CORE_BUNDLE_HEADINGS)
_ACTIVE_PROJECTS_SLUG = slugify(ACTIVE_PROJECTS_HEADING)
_RECENT_THEMES_SLUG = slugify(RECENT_THEMES_HEADING)

# Small non-core sections we surface whole (no H3 entries to target within them).
# Recent Themes is the canonical example: short, and relevant to current work.
_SMALL_WHOLE_SECTION_SLUGS: frozenset[str] = frozenset({_RECENT_THEMES_SLUG})

_CURRENT_WORK_KEYWORDS: tuple[str, ...] = (
    "today",
    "working",
    "wip",
    "in progress",
    "current",
    "now",
    "pr ",
    "release",
    "commit",
    "deploy",
    "branch",
)

_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]{1,}")


@dataclass(frozen=True)
class RouteResult:
    """The sections/subsections an observation chunk impacts.

    ``section_handles`` are H2 sections surfaced in full (the core bundle and any
    small whole-section like Recent Themes). ``subsection_handles`` are individual
    H3 entries (one project / archived item) surfaced WITHOUT their whole parent
    H2, so the per-fold context stays proportional to the touched work.

    ``unmatched_name_tokens`` are name-ish tokens in the chunk that did NOT match
    any existing section/subsection — a strong signal the observation concerns a
    brand-new project/repo that should be ADDED (NEW_AFTER) rather than folded
    into an unrelated existing entry. ``rotation_only`` is True when the only
    surfaced entry came from the deterministic rotation fallback (nothing in the
    chunk actually matched), so the caller can avoid advertising an arbitrary,
    unrelated entry as patchable.
    """

    section_handles: list[str] = field(default_factory=list)
    subsection_handles: list[str] = field(default_factory=list)
    unmatched_name_tokens: list[str] = field(default_factory=list)
    rotation_only: bool = False


def core_bundle_handles(document: ReflectionDocument) -> list[str]:
    """Handles of the always-visible core-bundle sections present in *document*."""
    return [section.handle for section in document.sections if section.slug in _CORE_SLUGS]


def _name_tokens(text: str) -> set[str]:
    """Lowercased slugified name-ish tokens found in an observation chunk.

    A compound name like ``hermes-agent`` or ``om-relay`` is kept WHOLE (the full
    slug) *and* split into its hyphen components (``hermes``, ``agent`` / ``om``,
    ``relay``). Users routinely abbreviate a project by one component ("worked on
    hermes today"), so matching a component against its parent project is what
    keeps those updates routed to the right H3 entry instead of misrouted into
    Recent Themes / core.
    """
    tokens: set[str] = set()
    for match in _NAME_TOKEN_RE.finditer(text):
        raw = match.group(0)
        for piece in re.split(r"[/.\-]", raw):
            slug = slugify(piece)
            if len(slug) >= 3:
                tokens.add(slug)
        # Keep the whole compound slug too (e.g. ``hermes-agent``) so an exact
        # full-name reference still matches.
        whole = slugify(raw)
        if len(whole) >= 3:
            tokens.add(whole)
    return tokens


def _is_about_current_work(chunk_lower: str) -> bool:
    return any(keyword in chunk_lower for keyword in _CURRENT_WORK_KEYWORDS)


def _all_subsections(document: ReflectionDocument) -> list[Subsection]:
    """Every H3 subsection across the heavy (non-core) H2 sections, in order."""
    subs: list[Subsection] = []
    for section in document.sections:
        if section.slug in _CORE_SLUGS:
            continue
        subs.extend(section.subsections)
    return subs


def _subsection_match_tokens(heading: str, slug: str) -> set[str]:
    """Match tokens for an H3 heading.

    Split on slashes, dots, hyphens, AND whitespace so a compound project name
    like ``hermes-agent`` contributes both its whole slug and each component
    (``hermes``, ``agent``). This lets an abbreviated chunk reference ("hermes")
    match its parent project entry.
    """
    sub_tokens = {slugify(part) for part in re.split(r"[/.\-\s]", heading) if part}
    sub_tokens.add(slug)
    # Also add the hyphen components of the slug itself.
    sub_tokens.update(slugify(part) for part in slug.split("-") if len(part) >= 2)
    return {t for t in sub_tokens if t}


def _matching_subsections(document: ReflectionDocument, tokens: set[str]) -> list[Subsection]:
    """H3 entries whose heading/name tokens appear in the chunk's name tokens."""
    if not tokens:
        return []
    matched: list[Subsection] = []
    for sub in _all_subsections(document):
        sub_tokens = _subsection_match_tokens(sub.heading, sub.slug)
        if tokens & sub_tokens:
            matched.append(sub)
    return matched


def _small_whole_sections(document: ReflectionDocument) -> list[Section]:
    return [s for s in document.sections if s.slug in _SMALL_WHOLE_SECTION_SLUGS]


def route_chunk(
    document: ReflectionDocument,
    chunk: str,
    *,
    fold_index: int,
    fold_total: int,
) -> RouteResult:
    """Route an observation *chunk* to the sections/subsections it impacts.

    Deterministic, no LLM call. The result ALWAYS includes the core bundle, then
    adds:

      - ``Recent Themes`` (whole) when the chunk reads as current-work;
      - the H2 of any small whole-section directly named in the chunk;
      - any H3 project/archive entry whose name/repo/path appears in the chunk;
      - a heading match: any non-core H2 whose heading slug is referenced
        (surfaced whole, since it was named directly);
      - a deterministic rotation fallback: when nothing else matched a touched
        entry, pick ONE H3 entry by ``fold_index`` so that across folds the per-
        fold context reaches PAST the document head (legacy never does). When the
        document has no H3 entries at all, fall back to rotating one non-core H2.

    Subsection handles are surfaced WITHOUT their whole parent H2, so the per-fold
    context is proportional to the touched work, not the document size.
    """
    section_handles: set[str] = set(core_bundle_handles(document))
    subsection_handles: set[str] = set()

    chunk_lower = chunk.lower()
    tokens = _name_tokens(chunk)

    handles_by_slug = {section.slug: section.handle for section in document.sections}
    non_core = [section for section in document.sections if section.slug not in _CORE_SLUGS]

    matched_touched = False
    # Track which chunk tokens actually matched an existing section/subsection so
    # we can tell the caller which look like brand-new project/repo names.
    matched_tokens: set[str] = set()

    # Recent Themes (whole) when the update is about current work.
    if _is_about_current_work(chunk_lower) and _RECENT_THEMES_SLUG in handles_by_slug:
        section_handles.add(handles_by_slug[_RECENT_THEMES_SLUG])
        matched_touched = True

    # Small whole-sections named directly in the chunk.
    for section in _small_whole_sections(document):
        if section.slug in tokens:
            section_handles.add(section.handle)
            matched_touched = True
            matched_tokens.add(section.slug)

    # Matching H3 project/archive entries by repo/project name.
    for sub in _matching_subsections(document, tokens):
        subsection_handles.add(sub.handle)
        matched_touched = True
        matched_tokens |= tokens & _subsection_match_tokens(sub.heading, sub.slug)

    # Direct heading match: a non-core H2 named in the chunk (surfaced whole).
    for section in non_core:
        if section.slug in tokens and section.slug not in _SMALL_WHOLE_SECTION_SLUGS:
            section_handles.add(section.handle)
            matched_touched = True
            matched_tokens.add(section.slug)

    # Tokens that matched no existing section/subsection. When the chunk names a
    # brand-new project/repo, these are how the caller knows to steer toward a
    # NEW_AFTER addition instead of an unrelated rotation sibling.
    unmatched = sorted(tokens - matched_tokens)

    rotation_only = False
    # Deterministic rotation fallback: surface ONE touched entry per fold so
    # coverage reaches past the head across folds. This is coverage, not a real
    # match — flag it so the caller does not advertise the arbitrary entry as
    # patchable (which would bias the model toward editing the wrong entry).
    if not matched_touched:
        rotation_only = True
        all_subs = _all_subsections(document)
        if all_subs:
            subsection_handles.add(all_subs[fold_index % len(all_subs)].handle)
        elif non_core:
            section_handles.add(non_core[fold_index % len(non_core)].handle)

    order = {section.handle: section.order for section in document.sections}
    sub_order = {
        sub.handle: (section.order, sub_index)
        for section in document.sections
        for sub_index, sub in enumerate(section.subsections)
    }
    return RouteResult(
        section_handles=sorted(section_handles, key=lambda h: order.get(h, 1_000_000)),
        subsection_handles=sorted(subsection_handles, key=lambda h: sub_order.get(h, (1_000_000, 0))),
        unmatched_name_tokens=unmatched,
        rotation_only=rotation_only,
    )
