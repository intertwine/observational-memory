"""Inline metadata helpers for reflection entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

_META_RE = re.compile(r"\s*<!--om:\s*(.*?)\s*-->\s*$")
_BULLET_RE = re.compile(r"^(\s*[-*]\s+)(.*?)(\s*<!--om:\s*.*?\s*-->\s*)?$")
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
# A section-level provenance marker. Distinct prefix from `<!--om:` so the
# per-bullet pass (`_META_RE`/`_BULLET_RE`/`parse_metadata`) never sees it: a
# section marker is invisible to `ensure_reflection_metadata` (falls through the
# `if not bullet` branch as a verbatim passthrough) and to the line-based cluster
# and host scope filters (`parse_metadata` returns `{}` for it, so it carries no
# `scope` key and can never be dropped). It lives on its own line immediately
# after a `## ` heading so it rides inside `Section.text` and reassembles
# byte-for-byte; it can never match `_H2_RE`, so it creates no spurious section.
_SECTION_META_RE = re.compile(r"^\s*<!--om-section:\s*(.*?)\s*-->\s*$")


@dataclass(frozen=True)
class PruneSummary:
    pruned: int = 0
    annotated: int = 0
    stale_sectioned: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "pruned": self.pruned,
            "annotated": self.annotated,
            "stale_sectioned": self.stale_sectioned,
        }


@dataclass(frozen=True)
class ReflectionConflict:
    section: str
    kind: str
    actionability: str
    entries: list[dict[str, str]]

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "kind": self.kind,
            "actionability": self.actionability,
            "entries": self.entries,
        }


def ensure_reflection_metadata(
    text: str,
    *,
    now: datetime | None = None,
    node: str = "local",
    source_mtime: datetime | None = None,
) -> str:
    now_value = _iso(now or datetime.now(timezone.utc))
    legacy_seen_value = _iso(source_mtime) if source_mtime is not None else now_value
    lines = text.splitlines()
    current_section = ""
    output: list[str] = []
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            current_section = heading.group(2).strip()
            output.append(line)
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            output.append(line)
            continue
        prefix, body, _comment = bullet.groups()
        body = body.rstrip()
        if not body:
            output.append(line)
            continue
        fields = parse_metadata(line)
        kind = fields.setdefault("kind", infer_kind(body, current_section))
        fields.setdefault("id", _entry_id(body))
        fields.setdefault("last_seen", legacy_seen_value)
        fields.setdefault("node", node)
        fields.setdefault("scope", "cluster")
        for key, value in _default_fields(kind).items():
            fields.setdefault(key, value)
        output.append(f"{prefix}{body} {format_metadata(fields)}")
    return "\n".join(output).rstrip() + "\n"


def _split_h2_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split ``text`` into ``(heading_line, body_lines)`` per H2 section.

    Lines before the first H2 are returned under an empty heading key ``""`` so
    a preamble (``# Reflections`` + timestamp lines) round-trips. Each section's
    body runs up to (but excluding) the next H2 heading.
    """
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None and len(heading.group(1)) == 2:
            sections.append((current_heading, current_body))
            current_heading = line
            current_body = []
        else:
            current_body.append(line)
    sections.append((current_heading, current_body))
    return sections


def _section_body_without_markers(body_lines: list[str]) -> str:
    """A section body with every ``<!--om-section:`` marker line removed.

    Used as the comparison key for change-detection: two renderings of the same
    section are "unchanged" iff their non-marker bodies are byte-identical, so a
    differing-only provenance stamp never counts as a content change.
    """
    return "\n".join(line for line in body_lines if not _SECTION_META_RE.match(line))


def ensure_section_provenance(
    text: str,
    *,
    obs_window: tuple[str, str] | None,
    now: datetime | None = None,
    prior_text: str = "",
) -> str:
    """Stamp section-level rot-proof provenance onto TOUCHED H2 sections of ``text``.

    For every ``## `` (H2) section whose content CHANGED this run, emit exactly
    one
    ``<!--om-section: last_reflected=<date> derived_from_obs_window=<min>..<max>-->``
    line as the section's first body line, immediately after the heading, and
    drop EVERY pre-existing ``<!--om-section:`` marker anywhere in that section's
    body (self-healing: idempotent, and resilient to a model echoing the stamp
    on a reflowed line rather than heading-adjacent).

    HONEST ``last_reflected``: a section is "changed" iff its non-marker body
    differs from the same-named section in ``prior_text``. An UNCHANGED section
    keeps its prior marker VERBATIM — it was not touched by this reflect, so its
    ``last_reflected`` must not be refreshed. Under the sectioned/auto strategy
    the untouched sections are reassembled byte-for-byte from the prior document,
    so the vast majority of sections at scale correctly retain their honest prior
    stamp. When ``prior_text`` is empty (the default — used by the function's own
    unit tests and any single-pass caller that genuinely rewrote everything),
    every section is treated as changed and restamped, preserving prior behavior.

    ``obs_window`` is the ``(min, max)`` date range of the observation window
    actually folded this run. When it is ``None`` the text is returned
    BYTE-IDENTICAL (strict no-op) — this is the prune/idempotency guarantee: a
    caller with no real reflect window (e.g. ``om prune``) must never fabricate
    or refresh provenance. When the window is present but degenerate
    (``min == max``) a single-day range is stamped (honest, never empty).

    Only H2 headings are stamped; H3 (``### ``) subsection headers are left
    untouched so subsection slug/handle routing is unaffected. The pass is
    fail-closed: any unexpected line is emitted verbatim, and a document with no
    H2 headings returns unchanged.

    NOTE on the two ``last_reflected`` notions: this per-section
    ``last_reflected`` is the WALL-CLOCK UTC date the reflect RAN (``now``),
    answering "when was this section last touched by a reflect". It is
    deliberately distinct from the document-level ``*Last reflected:*`` line
    (stamped to the newest OBSERVATION date by ``_stamp_timestamps``); the two
    can differ by the observe->reflect lag. A synthetic ``## Stale snapshots``
    bucket that ``prune_stale_snapshots`` appends AFTER this pass is intentionally
    left unstamped (it is not derived from an observation window).
    """
    if obs_window is None:
        return text

    now_date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    window_min, window_max = obs_window
    marker = f"<!--om-section: last_reflected={now_date} derived_from_obs_window={window_min}..{window_max}-->"

    # Map prior heading -> non-marker body so an unchanged section is detected by
    # byte-equality of its content (the marker line itself is excluded).
    prior_bodies: dict[str, str] = {}
    for heading_line, body_lines in _split_h2_sections(prior_text):
        if heading_line:
            prior_bodies[heading_line] = _section_body_without_markers(body_lines)

    output: list[str] = []
    for heading_line, body_lines in _split_h2_sections(text):
        if not heading_line:
            # Preamble before the first H2 — emit verbatim, never stamped.
            output.extend(body_lines)
            continue
        current_body = _section_body_without_markers(body_lines)
        prior_body = prior_bodies.get(heading_line)
        unchanged = prior_body is not None and prior_body == current_body
        output.append(heading_line)
        if unchanged:
            # Section was not touched this run: preserve its body verbatim,
            # including any prior `<!--om-section:` marker (do NOT refresh).
            output.extend(body_lines)
        else:
            # Touched (or new) section: emit one fresh marker right after the
            # heading and drop every stale marker from the body (self-healing).
            output.append(marker)
            output.extend(line for line in body_lines if not _SECTION_META_RE.match(line))
    return "\n".join(output).rstrip() + "\n"


def parse_metadata(line: str) -> dict[str, str]:
    match = _META_RE.search(line)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for token in match.group(1).split():
        key, sep, value = token.partition("=")
        if sep and key:
            fields[key] = value
    return fields


def format_metadata(fields: dict[str, str]) -> str:
    ordered = [
        "id",
        "kind",
        "mode",
        "source_type",
        "confidence",
        "sensitivity",
        "actionability",
        "last_seen",
        "last_verified",
        "expires",
        "seen_count",
        "node",
        "scope",
    ]
    parts = []
    for key in ordered:
        if key in fields:
            parts.append(f"{key}={fields[key]}")
    for key in sorted(k for k in fields if k not in ordered):
        parts.append(f"{key}={fields[key]}")
    return "<!--om: " + " ".join(parts) + "-->"


def infer_kind(body: str, section: str = "") -> str:
    lower = f"{section} {body}".lower()
    if "working mode" in lower or "execution mode" in lower or "launch mode" in lower or "mode:" in lower:
        return "mode"
    if "identity" in lower or "name:" in lower or "role:" in lower:
        return "identity"
    if "policy" in lower or "must not" in lower or "never " in lower or "requires explicit approval" in lower:
        return "policy"
    if "preference" in lower or "prefers" in lower or "communication" in lower:
        return "preference"
    snapshot_markers = (
        "pr #",
        "issue #",
        "branch",
        "commit",
        "sha",
        "pending",
        "approved",
        "merged",
        "current",
        "today",
        "yesterday",
    )
    if any(marker in lower for marker in snapshot_markers):
        return "snapshot"
    if "task" in lower or "todo" in lower or "open question" in lower:
        return "task"
    if "decision" in lower or "decided" in lower:
        return "decision"
    return "evergreen"


# Gate-4 share-out allowlist SOCKET. This is the single, extensible policy that
# decides which EXPLICIT scope values may leave the host into shared cluster
# snapshots or the Moss cloud upload. HARD CONSTRAINT: it ships with EXACTLY one
# member, ``"cluster"``. Do NOT add inert ``team``/``org``/any value here — a
# future tier widens sharing by adding a member (or registering through the
# resolver) WITHOUT editing any leak-critical filter body. Adding a value before
# a tier enforces its visibility would silently share that scope off-host (the
# Moss path uploads plaintext to a cloud), an irreversible leak.
SHAREABLE_SCOPES: frozenset[str] = frozenset({"cluster"})


def _scope_is_shareable(scope: str | None) -> bool:
    """Allowlist resolver for the leak-critical share-out paths (fails closed).

    - Absent scope (``None``) RIDES ALONG — structural lines (headings, prose,
      blank lines, ``<!--om-section:`` stamps, the ``*Last reflected:*`` preamble)
      and hand-typed unstamped bullets carry no scope key and are shared as today.
    - An EXPLICIT scope that is a member of :data:`SHAREABLE_SCOPES` is shared.
    - An EXPLICIT non-member scope is WITHHELD: ``scope=local``, a typo such as
      ``locol``, an LLM-hallucinated value, a future value, or a hand-typed
      ``team``/``org`` value a tier has not yet enabled all FAIL CLOSED.

    The ``scope is None`` disjunction is deliberate: it keeps absent (rides along)
    and explicit-empty (``scope=`` -> ``""``, withheld) distinct, so an empty
    value also fails closed. A future tier widens sharing purely by adding to
    :data:`SHAREABLE_SCOPES` — never by editing a filter body.
    """
    return scope is None or scope in SHAREABLE_SCOPES


def _entry_indent(line: str) -> int:
    """Leading-whitespace width of a line (its list-item / block indent)."""
    return len(line) - len(line.lstrip())


@dataclass
class _Structural:
    """A line that carries no scope of its own — heading, blank, marker, or prose
    OUTSIDE any scoped item. Always shareable on its own; pruned only if its
    enclosing section/entry is dropped."""

    line: str


@dataclass
class _Entry:
    """A reflection entry: a head line (a bullet OR a metadata-bearing prose line)
    plus the body it owns. ``scope`` is the head's explicit scope (``None`` when
    the head carries none). ``body`` is the recursively-parsed list of nodes the
    entry contains — its Markdown continuations, blank lines, and nested child
    blocks. Explicitly scoped children are their OWN ``_Entry`` nodes inside
    ``body`` and are judged on their own scope; unscoped body content inherits this
    entry's share decision."""

    head: str
    scope: str | None
    body: list


# --- Block IR ---------------------------------------------------------------
#
# Gate 4 enforces a BLOCK-level privacy rule (scope is attached to a memory
# ENTRY), so share-out parses the document into entries/blocks ONCE and applies
# the allowlist to entries, rather than scanning physical lines (which repeatedly
# leaked the next Markdown shape: indented, lazy, loose, and nested continuations).
# Membership is defined in exactly one place — :func:`_parse_reflection_blocks` —
# following the OM-supported CommonMark subset:
#
#   An entry owns every following line MORE indented than its head (indented
#   continuations and nested UNSCOPED child list items), same-indent "lazy"
#   continuation prose with no intervening blank, and — for a LOOSE list — content
#   that stays indented under the head across a blank line. It ENDS at a heading, a
#   section marker, a sibling/shallower list item, an explicitly scoped line (a new
#   entry, judged on its own scope), or a blank line that is followed by
#   dedented/boundary content (which RELEASES that following block as independent).


def _is_entry_head(line: str) -> bool:
    return ("scope" in parse_metadata(line)) or (_BULLET_RE.match(line) is not None)


def _parse_reflection_blocks(lines: list[str]) -> list:
    """Parse ``lines`` into a flat, in-order list of :class:`_Structural` /
    :class:`_Entry` nodes (entries recurse into their body). See the Block IR note."""
    nodes: list = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if line.strip() == "" or not _is_entry_head(line):
            nodes.append(_Structural(line))
            index += 1
            continue
        head_indent = _entry_indent(line)
        scope = parse_metadata(line).get("scope")
        # Gather the body's line range [index+1, cursor).
        cursor = index + 1
        while cursor < total:
            current = lines[cursor]
            if current.strip() == "":
                # A blank belongs to the item only for a LOOSE list — i.e. when a
                # later line stays indented under the head. Otherwise the blank is a
                # hard boundary and the following block is released.
                look = cursor
                while look < total and lines[look].strip() == "":
                    look += 1
                if look < total and _entry_indent(lines[look]) > head_indent:
                    cursor = look  # blanks + deeper content stay in the body
                    continue
                break
            if _entry_indent(current) > head_indent:
                cursor += 1  # deeper content: indented continuation / nested child
                continue
            # Same-or-shallower, non-blank, no intervening blank: a sibling/boundary
            # ends the entry; otherwise it is a lazy continuation of the head's text.
            if (
                _BULLET_RE.match(current) is not None
                or "scope" in parse_metadata(current)
                or _HEADING_RE.match(current) is not None
                or _SECTION_META_RE.match(current) is not None
            ):
                break
            cursor += 1
        nodes.append(_Entry(head=line, scope=scope, body=_parse_reflection_blocks(lines[index + 1 : cursor])))
        index = cursor
    return nodes


def _render_shareable(nodes: list) -> list[str]:
    """Render nodes whose governing entry is shareable. Structural lines ride along;
    each entry is judged on its OWN scope (children recurse on their own scope)."""
    out: list[str] = []
    for node in nodes:
        if isinstance(node, _Structural):
            out.append(node.line)
        elif _scope_is_shareable(node.scope):
            out.append(node.head)
            out.extend(_render_shareable(node.body))
        else:
            out.extend(_render_withheld_descendants(node.body))
    return out


def _render_withheld_descendants(nodes: list) -> list[str]:
    """Render survivors inside a WITHHELD entry: unscoped content (structural lines
    and unscoped child entries) is dropped with its parent; an explicitly scoped
    descendant is judged on its own scope, so a shareable scoped child survives."""
    out: list[str] = []
    for node in nodes:
        if isinstance(node, _Structural):
            continue
        if node.scope is None:
            out.extend(_render_withheld_descendants(node.body))
        elif _scope_is_shareable(node.scope):
            out.append(node.head)
            out.extend(_render_shareable(node.body))
        else:
            out.extend(_render_withheld_descendants(node.body))
    return out


def filter_reflection_document_for_shareout(text: str) -> str:
    """THE single share-out filter: drop every entry whose scope is not shareable
    (with its unscoped continuations and nested children) before reflection memory
    leaves the host into shared cluster snapshots or the Moss cloud upload.

    Scope is attached to an ENTRY, not a physical line, so this parses the document
    into a block IR (:func:`_parse_reflection_blocks`), applies the
    :data:`SHAREABLE_SCOPES` allowlist via :func:`_scope_is_shareable` to each
    entry, then prunes any heading section left empty (:func:`_drop_empty_heading_sections`).

    Allowlist semantics (default ``{"cluster"}``, fail-closed):

    - ``scope=cluster`` entries are shared; ``scope=local`` and any EXPLICIT
      unknown scope (a typo, an LLM-hallucinated value, a hand-typed ``team`` /
      ``org`` a tier has not yet enabled) are WITHHELD.
    - Absent-scope structure — headings, blank lines, ``<!--om-section:`` stamps,
      the ``*Last reflected:*`` preamble, and prose OUTSIDE any scoped item — rides
      along. An absent-scope under-share self-heals on the next reflect (which
      setdefaults ``scope=cluster``); an explicit-unknown scope does not self-heal.
    - A withheld entry takes its indented/lazy/loose continuations and nested
      UNSCOPED child blocks with it (so wrapped or nested private text never leaks),
      while an explicitly scoped child is judged on its own scope.

    A future visibility tier widens sharing solely by adding a member to
    :data:`SHAREABLE_SCOPES` — never by editing this filter or a caller.
    """
    nodes = _parse_reflection_blocks(text.splitlines())
    kept = _render_shareable(nodes)
    kept = _drop_empty_heading_sections(kept)
    return "\n".join(kept).rstrip() + "\n"


def filter_reflection_entries_for_cluster(text: str) -> str:
    """Back-compat name for :func:`filter_reflection_document_for_shareout` (the
    cluster snapshot share-out path)."""
    return filter_reflection_document_for_shareout(text)


def _line_is_real_content(line: str) -> bool:
    """True if ``line`` is durable shared content — not a heading, blank, or marker.

    Headings (any level) are *structure*, a provenance stamp is *metadata*, and a
    blank line is *whitespace*; none of them keeps a section alive on their own.
    Only a real bullet/prose line counts, so an H2/H3/H4 heading whose body was
    entirely ``scope=local`` is correctly treated as empty.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _SECTION_META_RE.match(line):
        return False
    if _HEADING_RE.match(line):
        return False
    return True


def _drop_empty_heading_sections(lines: list[str]) -> list[str]:
    """Recursively drop any heading block with no real content after filtering.

    Operates at every heading level (H2 through H6), not just H2: a section is
    "empty" when, after local-line filtering, its block (down to the next heading
    of the same-or-shallower level) contains no real content line — where a
    nested sub-heading only counts if *it* survives pruning. Such a block's
    heading and any ``<!--om-section:`` stamp are removed entirely.

    This closes the subsection leak: an H3/H4 whose every bullet was
    ``scope=local`` is dropped along with its title, and an H2 that is left with
    only an empty private subsection is dropped too. Lines before the first
    heading (preamble) and any block with surviving content are kept verbatim.
    """
    if not lines:
        return []
    levels = [len(m.group(1)) for line in lines if (m := _HEADING_RE.match(line))]
    if not levels:
        # No headings here — these are leaf content/blank/marker lines; keep as-is.
        return list(lines)
    top = min(levels)

    output: list[str] = []
    index = 0
    total = len(lines)
    # Preamble: everything before the first top-level heading is kept verbatim.
    while index < total:
        heading = _HEADING_RE.match(lines[index])
        if heading is not None and len(heading.group(1)) == top:
            break
        output.append(lines[index])
        index += 1
    # Each top-level heading + its block (down to the next same-level heading).
    while index < total:
        heading_line = lines[index]
        index += 1
        block: list[str] = []
        while index < total:
            heading = _HEADING_RE.match(lines[index])
            if heading is not None and len(heading.group(1)) == top:
                break
            block.append(lines[index])
            index += 1
        pruned = _drop_empty_heading_sections(block)
        if any(_line_is_real_content(line) for line in pruned):
            output.append(heading_line)
            output.extend(pruned)
        # else: wholly-local / empty block — drop heading + stamp + body entirely.
    return output


def filter_reflection_entries_for_host(text: str, *, local_node: str) -> str:
    """Hide remote host-local entries when materializing generated Markdown."""
    output: list[str] = []
    for line in text.splitlines():
        fields = parse_metadata(line)
        if fields.get("scope") == "local" and fields.get("node") not in {"", local_node}:
            continue
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


# High-stakes kinds whose disagreement is worth surfacing to the operator even
# at low actionability. Shared by the cross-host conflict heuristic and the
# single-host prior-vs-new diff so the "what counts as reviewable" gate can never
# drift between the two callers.
_HIGH_STAKES_KINDS = frozenset({"identity", "preference", "policy", "decision", "mode"})


def _is_reviewable_entry(*, scope: str | None, kind: str, actionability: str) -> bool:
    """Whether a reflection bullet is worth comparing for conflicts.

    Local-scoped and snapshot (volatile operational) entries are never
    reviewable. Otherwise an entry qualifies only when it is a high-stakes kind
    or carries high actionability — the same gate the cluster heuristic and the
    prior-vs-new diff share, so neither can silently widen or narrow it.
    """
    if scope == "local" or kind == "snapshot":
        return False
    if kind not in _HIGH_STAKES_KINDS and actionability != "high":
        return False
    return True


def find_reflection_conflicts(snapshots: list[tuple[str, str, str]]) -> list[ReflectionConflict]:
    """Find operator-visible conflicts across reflection snapshot bodies.

    ``snapshots`` contains ``(record_id, node_id, body)`` tuples. This is a
    deterministic heuristic: non-snapshot entries in the same section/kind from
    different records are reviewable when their normalized text differs.
    """
    buckets: dict[tuple[str, str], list[dict[str, str]]] = {}
    for record_id, node_id, body in snapshots:
        section = ""
        for line in body.splitlines():
            heading = _HEADING_RE.match(line)
            if heading:
                section = heading.group(2).strip()
                continue
            bullet = _BULLET_RE.match(line)
            if not bullet:
                continue
            fields = parse_metadata(line)
            kind = fields.get("kind") or infer_kind(bullet.group(2), section)
            actionability = fields.get("actionability", _default_fields(kind)["actionability"])
            if not _is_reviewable_entry(scope=fields.get("scope"), kind=kind, actionability=actionability):
                continue
            text = bullet.group(2).strip()
            key = (section, kind)
            buckets.setdefault(key, []).append(
                {
                    "record_id": record_id,
                    "node": fields.get("node", node_id),
                    "entry_id": fields.get("id", _entry_id(text)),
                    "actionability": actionability,
                    "text": text,
                }
            )
    conflicts: list[ReflectionConflict] = []
    for (section, kind), entries in sorted(buckets.items()):
        normalized = {entry["text"].casefold() for entry in entries}
        record_ids = {entry["record_id"] for entry in entries}
        if len(normalized) > 1 and len(record_ids) > 1:
            actionability = "high" if any(entry["actionability"] == "high" for entry in entries) else "medium"
            conflicts.append(
                ReflectionConflict(section=section, kind=kind, actionability=actionability, entries=entries)
            )
    return conflicts


def _iter_reviewable_entries(body: str) -> list[dict[str, str]]:
    """Walk a reflections document, yielding reviewable high-stakes entries.

    Each entry records its ``section``/``kind``/``actionability``, its stable
    ``id`` (the explicit ``id=`` metadata when present, else a text hash) plus an
    ``id_explicit`` flag, and the casefold-able ``text``. The gate matches
    :func:`find_reflection_conflicts` exactly via :func:`_is_reviewable_entry`.
    """
    return [entry for entry in _iter_all_entries(body) if entry["reviewable"]]


def _iter_all_entries(body: str) -> list[dict[str, str]]:
    """Walk a reflections document, yielding EVERY non-empty bullet with a
    ``reviewable`` flag set from :func:`_is_reviewable_entry`. The reviewable-only
    walk filters this; the solo-section downgrade signal needs the full set so it
    can tell a one-bullet section from a multi-bullet one regardless of kind."""
    entries: list[dict[str, str]] = []
    section = ""
    for line in body.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(2).strip()
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        text = bullet.group(2).strip()
        if not text:
            continue
        fields = parse_metadata(line)
        kind = fields.get("kind") or infer_kind(text, section)
        actionability = fields.get("actionability", _default_fields(kind)["actionability"])
        explicit_id = fields.get("id")
        entries.append(
            {
                "section": section,
                "kind": kind,
                "actionability": actionability,
                "node": fields.get("node", ""),
                "id": explicit_id or _entry_id(text),
                "id_explicit": "1" if explicit_id else "",
                "text": text,
                "reviewable": "1"
                if _is_reviewable_entry(scope=fields.get("scope"), kind=kind, actionability=actionability)
                else "",
            }
        )
    return entries


# Punctuation/emphasis that a reflector freely adds or restyles without changing
# meaning. Normalizing it away before comparison keeps the diff from firing on
# cosmetic churn (bolding a name, curling a quote, an extra space, a trailing
# period) — the precision the advisory lives or dies by.
_COMPARE_TRANSLATION = {
    ord("‘"): "'",
    ord("’"): "'",
    ord("“"): '"',
    ord("”"): '"',
    ord("–"): "-",
    ord("—"): "-",
}


def _normalize_for_compare(text: str) -> str:
    """Canonicalize a bullet's text for divergence comparison.

    Normalizes smart quotes/dashes, strips Markdown emphasis (``*`` and
    backticks), collapses internal whitespace, drops trailing sentence
    punctuation, and casefolds — applied symmetrically to both sides so only a
    genuine wording change reads as a divergence. Deliberately conservative: it
    does NOT touch ``_`` (snake_case) to avoid collapsing distinct facts.
    """
    text = text.translate(_COMPARE_TRANSLATION)
    text = text.replace("*", "").replace("`", "")
    text = " ".join(text.split())
    return text.strip().rstrip(".,;:!?").casefold()


def _diff_conflict(prior_entry: dict[str, str], new_entry: dict[str, str], *, signal: str) -> ReflectionConflict:
    actionability = "high" if "high" in {prior_entry["actionability"], new_entry["actionability"]} else "medium"
    return ReflectionConflict(
        section=prior_entry["section"],
        kind=prior_entry["kind"],
        actionability=actionability,
        entries=[
            {
                "side": "prior",
                "node": prior_entry["node"],
                "entry_id": prior_entry["id"],
                "actionability": prior_entry["actionability"],
                "text": prior_entry["text"],
                "signal": signal,
            },
            {
                "side": "new",
                "node": new_entry["node"],
                "entry_id": new_entry["id"],
                "actionability": new_entry["actionability"],
                "text": new_entry["text"],
                "signal": signal,
            },
        ],
    )


def diff_reflection_conflicts(prior: str, new: str) -> list[ReflectionConflict]:
    """Surface high-stakes facts a single reflect cycle silently changed.

    Compares the prior on-disk reflections document against the freshly reflected
    one. ``find_reflection_conflicts`` is built for the cross-host case (it needs
    two or more records to fire), so it returns nothing over one host's single
    document; this diff is the single-host complement.

    Three high-precision, false-positive-safe signals fire. Every text
    comparison runs through :func:`_normalize_for_compare`, so cosmetic restyling
    (bold, smart quotes, whitespace, a trailing period) never counts as a change.

    * **id-divergence** — an explicit ``id=`` present in BOTH documents whose text
      diverged. Unambiguous, but only when the reflector echoed the entry's
      metadata comment across the edit.
    * **singleton-slot divergence** — a high-stakes ``(section, kind)`` slot
      holding exactly one reviewable entry on each side whose text differs.
      Robust to id loss; targets a single Name / role / governing policy quietly
      reworded. Requires one-to-one (never fires on a slot that gained/lost
      entries).
    * **solo-section downgrade** — a section that is a single bullet on BOTH
      sides where the PRIOR bullet was high-stakes and the text diverged,
      regardless of the NEW bullet's kind/scope. This is the load-bearing guard
      against a guardrail being silently *loosened*: a reflector can reword
      "deploys must not run without approval" into "deploys may run" and re-tag it
      ``evergreen``/``scope=local``, which would otherwise drop it from review.
      Anchoring reviewability on the prior side (fixed history, not this run's
      model-chosen classification) closes that without inviting false positives.

    Known residual (deferred, by design — precision over recall): a high-stakes
    fact reworded *inside a multi-bullet section* while its kind changes is not
    caught (the one-to-one anchor can't disambiguate it from an add/drop). Silent
    drops are likewise not reported — under metadata-comment churn an edit can
    masquerade as a drop, a false positive.

    Read-only and advisory: callers must never mutate durable Markdown from this.
    """
    prior_entries = _iter_reviewable_entries(prior)
    new_entries = _iter_reviewable_entries(new)

    conflicts: list[ReflectionConflict] = []
    # Signals 1 and 2 distinguish slots by (section, kind), so independent
    # high-stakes facts of different kinds under one heading each report. Signal 3
    # operates on solo (single-bullet) sections, where there is exactly one slot,
    # so it dedupes at section granularity against whatever already fired there.
    reported_slots: set[tuple[str, str]] = set()
    reported_sections: set[str] = set()

    def _record(prior_entry: dict[str, str], new_entry: dict[str, str], *, signal: str) -> None:
        conflicts.append(_diff_conflict(prior_entry, new_entry, signal=signal))
        reported_slots.add((prior_entry["section"], prior_entry["kind"]))
        reported_sections.add(prior_entry["section"])

    # Signal 1: same explicit id on both sides, text diverged.
    prior_by_id = {e["id"]: e for e in prior_entries if e["id_explicit"]}
    new_by_id = {e["id"]: e for e in new_entries if e["id_explicit"]}
    for entry_id in sorted(prior_by_id.keys() & new_by_id.keys()):
        prior_entry = prior_by_id[entry_id]
        new_entry = new_by_id[entry_id]
        if _normalize_for_compare(prior_entry["text"]) != _normalize_for_compare(new_entry["text"]):
            _record(prior_entry, new_entry, signal="id")

    # Signal 2: singleton high-stakes (section, kind) slot on each side, diverged.
    prior_slots = _singleton_slots(prior_entries)
    new_slots = _singleton_slots(new_entries)
    for key in sorted(prior_slots.keys() & new_slots.keys()):
        if key in reported_slots:
            continue
        prior_entry = prior_slots[key]
        new_entry = new_slots[key]
        if _normalize_for_compare(prior_entry["text"]) != _normalize_for_compare(new_entry["text"]):
            _record(prior_entry, new_entry, signal="slot")

    # Signal 3: solo-section downgrade — prior-anchored, so a new-side
    # reclassification can't hide a reworded guardrail.
    prior_solo = _solo_sections(prior, require_reviewable=True)
    new_solo = _solo_sections(new, require_reviewable=False)
    for section in sorted(prior_solo.keys() & new_solo.keys()):
        if section in reported_sections:
            continue
        prior_entry = prior_solo[section]
        new_entry = new_solo[section]
        if _normalize_for_compare(prior_entry["text"]) != _normalize_for_compare(new_entry["text"]):
            _record(prior_entry, new_entry, signal="downgrade")
    return conflicts


def _solo_sections(body: str, *, require_reviewable: bool) -> dict[str, dict[str, str]]:
    """Map each section that holds exactly one non-empty bullet to that bullet.

    When ``require_reviewable`` is set, only sections whose single bullet is a
    high-stakes reviewable entry are returned (the prior-side anchor); otherwise
    any single-bullet section qualifies (the new side, so a downgraded bullet
    still pairs)."""
    by_section: dict[str, list[dict[str, str]]] = {}
    for entry in _iter_all_entries(body):
        by_section.setdefault(entry["section"], []).append(entry)
    solo: dict[str, dict[str, str]] = {}
    for section, members in by_section.items():
        if len(members) != 1:
            continue
        only = members[0]
        if require_reviewable and not only["reviewable"]:
            continue
        solo[section] = only
    return solo


def _singleton_slots(entries: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    """Map ``(section, kind)`` to its sole reviewable entry, omitting slots with
    zero or more than one entry (a one-to-one slot is the only safe place to read
    a text change as a divergence rather than an addition/removal)."""
    by_slot: dict[tuple[str, str], list[dict[str, str]]] = {}
    for entry in entries:
        by_slot.setdefault((entry["section"], entry["kind"]), []).append(entry)
    return {key: members[0] for key, members in by_slot.items() if len(members) == 1}


def prune_stale_snapshots(
    text: str,
    *,
    now: datetime | None = None,
    ttl_days: int = 14,
    action: str = "stale-section",
) -> tuple[str, PruneSummary]:
    if action not in {"stale-section", "drop", "annotate"}:
        raise ValueError(f"Unsupported snapshot expiry action: {action}")
    now_dt = now or datetime.now(timezone.utc)
    current_section = ""
    output: list[str] = []
    stale: list[str] = []
    summary = PruneSummary()
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current_section = heading.group(2).strip()
            output.append(line)
            continue
        fields = parse_metadata(line)
        if fields.get("kind") != "snapshot" or current_section.lower() == "stale snapshots":
            output.append(line)
            continue
        if not _is_stale(fields.get("last_seen"), now_dt, ttl_days):
            output.append(line)
            continue
        if action == "drop":
            summary = PruneSummary(
                pruned=summary.pruned + 1,
                annotated=summary.annotated,
                stale_sectioned=summary.stale_sectioned,
            )
            continue
        if action == "annotate":
            if "stale=true" not in line:
                line = line.rstrip() + " <!-- stale=true -->"
            output.append(line)
            summary = PruneSummary(
                pruned=summary.pruned,
                annotated=summary.annotated + 1,
                stale_sectioned=summary.stale_sectioned,
            )
            continue
        stale.append(line)
        summary = PruneSummary(
            pruned=summary.pruned,
            annotated=summary.annotated,
            stale_sectioned=summary.stale_sectioned + 1,
        )
    if stale:
        while output and output[-1] == "":
            output.pop()
        output.extend(["", "## Stale snapshots", "", *stale])
    return "\n".join(output).rstrip() + "\n", summary


def _is_stale(value: str | None, now: datetime, ttl_days: int) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).days >= ttl_days


def _entry_id(body: str) -> str:
    return "ome_" + sha256(body.strip().encode("utf-8")).hexdigest()[:16]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_fields(kind: str) -> dict[str, str]:
    actionability = {
        "identity": "high",
        "policy": "high",
        "preference": "medium",
        "decision": "medium",
        "task": "medium",
        "mode": "high",
        "snapshot": "low",
        "evergreen": "medium",
    }.get(kind, "medium")
    sensitivity = "normal"
    if kind in {"identity", "policy"}:
        sensitivity = "personal"
    return {
        "source_type": "inferred",
        "confidence": "medium",
        "sensitivity": sensitivity,
        "actionability": actionability,
        "seen_count": "1",
    }
