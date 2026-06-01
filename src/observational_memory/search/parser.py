"""Parse memory files into searchable documents."""

from __future__ import annotations

import re
from pathlib import Path

from . import Document, DocumentSource


def _line_number_for_offset(content: str, offset: int) -> int:
    """Return the 1-based line number for a character offset into content."""
    return content.count("\n", 0, offset) + 1


def derive_section_provenance(content: str) -> tuple[str | None, str | None, str | None]:
    """Reduce a section's bullet metadata to typed ``(owner, scope, source_type)``.

    The inline ``<!--om:-->`` metadata stays authoritative; these are derived
    typed labels that ride on the retrieval object. Reductions:

    - ``scope`` is MOST-RESTRICTIVE-WINS: ``"local"`` if ANY bullet is local,
      else ``"cluster"`` if any bullet declares cluster, else None. This keeps
      the typed label leak-safe (it can never under-claim sensitivity), but it
      is a LABEL only — the per-line strip is the sole upload gate.
    - ``owner`` (from inline ``node``) and ``source_type`` are set only when ALL
      non-empty values agree (homogeneous); otherwise None (an honest default
      when bullets disagree).

    Fail-closed: any parse error yields all-None rather than raising.
    """
    try:
        from ..reflection_metadata import parse_metadata
    except Exception:  # pragma: no cover - defensive
        return None, None, None

    scope: str | None = None
    owners: set[str] = set()
    source_types: set[str] = set()
    try:
        for line in content.splitlines():
            fields = parse_metadata(line)
            if not fields:
                continue
            line_scope = fields.get("scope")
            if line_scope == "local":
                scope = "local"
            elif line_scope == "cluster" and scope is None:
                scope = "cluster"
            owner_value = fields.get("node")
            if owner_value:
                owners.add(owner_value)
            source_type_value = fields.get("source_type")
            if source_type_value:
                source_types.add(source_type_value)
    except Exception:  # pragma: no cover - defensive
        return None, None, None

    owner = next(iter(owners)) if len(owners) == 1 else None
    source_type = next(iter(source_types)) if len(source_types) == 1 else None
    return owner, scope, source_type


def parse_auto_memory(claude_projects_dir: Path) -> list[Document]:
    """Parse all Claude Code auto-memory files into searchable Documents.

    Args:
        claude_projects_dir: Path to ``~/.claude/projects/``.

    Returns:
        List of Documents from all project memory directories.
    """
    from ..transcripts.auto_memory import scan_all_auto_memory

    return scan_all_auto_memory(claude_projects_dir)


def parse_observations(path: Path) -> list[Document]:
    """Split observations.md into one Document per date section."""
    if not path.exists():
        return []
    content = path.read_text()
    matches = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2})", content, flags=re.MULTILINE))

    documents = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[start:end].strip()
        if not section:
            continue

        date = match.group(1)
        documents.append(
            Document(
                doc_id=f"obs:{date}",
                source=DocumentSource.OBSERVATIONS,
                heading=f"## {date}",
                content=section,
                date=date,
                metadata={
                    "file_path": str(path),
                    "source_start_line": _line_number_for_offset(content, start),
                },
            )
        )
    return documents


def parse_reflections(path: Path) -> list[Document]:
    """Split reflections.md into one Document per top-level section."""
    if not path.exists():
        return []
    content = path.read_text()
    matches = list(re.finditer(r"^## (.+)", content, flags=re.MULTILINE))

    documents = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[start:end].strip()
        if not section:
            continue

        heading = match.group(1).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        owner, scope, source_type = derive_section_provenance(section)
        documents.append(
            Document(
                doc_id=f"ref:{slug}",
                source=DocumentSource.REFLECTIONS,
                heading=f"## {heading}",
                content=section,
                metadata={
                    "file_path": str(path),
                    "source_start_line": _line_number_for_offset(content, start),
                },
                owner=owner,
                scope=scope,
                source_type=source_type,
            )
        )
    return documents
