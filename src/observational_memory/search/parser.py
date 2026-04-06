"""Parse memory files into searchable documents."""

from __future__ import annotations

import re
from pathlib import Path

from . import Document, DocumentSource


def _line_number_for_offset(content: str, offset: int) -> int:
    """Return the 1-based line number for a character offset into content."""
    return content.count("\n", 0, offset) + 1


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
            )
        )
    return documents
