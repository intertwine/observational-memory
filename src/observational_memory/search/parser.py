"""Parse memory files into searchable documents."""

from __future__ import annotations

import re
from pathlib import Path

from . import Document, DocumentSource


def parse_observations(path: Path) -> list[Document]:
    """Split observations.md into one Document per date section."""
    if not path.exists():
        return []
    content = path.read_text()
    # Same regex used in reflect.py:_trim_old_observations()
    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", content, flags=re.MULTILINE)

    documents = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        date_match = re.match(r"## (\d{4}-\d{2}-\d{2})", section)
        if date_match:
            date = date_match.group(1)
            documents.append(
                Document(
                    doc_id=f"obs:{date}",
                    source=DocumentSource.OBSERVATIONS,
                    heading=f"## {date}",
                    content=section,
                    date=date,
                )
            )
    return documents


def parse_reflections(path: Path) -> list[Document]:
    """Split reflections.md into one Document per top-level section."""
    if not path.exists():
        return []
    content = path.read_text()
    sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)

    documents = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        heading_match = re.match(r"## (.+)", section)
        if heading_match:
            heading = heading_match.group(1).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
            documents.append(
                Document(
                    doc_id=f"ref:{slug}",
                    source=DocumentSource.REFLECTIONS,
                    heading=f"## {heading}",
                    content=section,
                )
            )
    return documents
