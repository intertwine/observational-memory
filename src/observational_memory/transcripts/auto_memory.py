"""Scanner for Claude Code auto-memory files.

SAFETY: This module READS from auto-memory directories but NEVER WRITES to them.
Om enriches reflections FROM auto-memory. It never writes BACK to auto-memory.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..search import Document, DocumentSource


@dataclass
class MemoryFile:
    """A single auto-memory file with metadata."""

    path: Path
    project_slug: str
    content: str
    content_hash: str
    is_index: bool  # True for MEMORY.md


def find_memory_directories(claude_projects_dir: Path) -> list[Path]:
    """Discover all Claude Code project memory directories.

    Args:
        claude_projects_dir: Path to ``~/.claude/projects/``.

    Returns:
        Sorted list of memory directory paths that exist and contain files.
    """
    if not claude_projects_dir.is_dir():
        return []

    dirs = []
    for project_dir in sorted(claude_projects_dir.iterdir()):
        memory_dir = project_dir / "memory"
        if memory_dir.is_dir() and any(memory_dir.glob("*.md")):
            dirs.append(memory_dir)
    return dirs


def extract_project_slug(project_dir_name: str) -> str:
    """Extract a human-readable slug from a sanitized project directory name.

    Claude Code sanitizes paths by replacing ``/`` with ``-``, producing names
    like ``-Users-bryanyoung-experiments-hive-orchestrator``. This extracts the
    last two meaningful segments to produce ``hive-orchestrator``.

    Args:
        project_dir_name: The sanitized directory name (e.g. from ``~/.claude/projects/``).

    Returns:
        A short, readable project slug.
    """
    # Split on hyphens, filter empty segments
    parts = [p for p in project_dir_name.split("-") if p]
    if len(parts) >= 2:
        return "-".join(parts[-2:])
    elif parts:
        return parts[-1]
    return project_dir_name.strip("-") or "unknown"


def _content_hash(content: str) -> str:
    """Compute sha256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def scan_memory_files(memory_dir: Path) -> list[MemoryFile]:
    """Enumerate all .md files in a project's memory directory.

    Args:
        memory_dir: Path to a project's ``memory/`` directory.

    Returns:
        List of MemoryFile instances with content and hashes.
    """
    project_dir_name = memory_dir.parent.name
    slug = extract_project_slug(project_dir_name)

    files = []
    for md_path in sorted(memory_dir.glob("*.md")):
        if not md_path.is_file():
            continue
        content = md_path.read_text()
        if not content.strip():
            continue
        files.append(
            MemoryFile(
                path=md_path,
                project_slug=slug,
                content=content,
                content_hash=_content_hash(content),
                is_index=md_path.name == "MEMORY.md",
            )
        )
    return files


def _strip_frontmatter(content: str) -> tuple[dict, str]:
    """Strip optional YAML frontmatter from markdown content.

    Args:
        content: Raw markdown text, possibly starting with ``---``.

    Returns:
        Tuple of (metadata_dict, body_text). If no frontmatter, metadata is empty.
    """
    if not content.startswith("---"):
        return {}, content

    # Find closing ---
    end = content.find("---", 3)
    if end == -1:
        return {}, content

    frontmatter_text = content[3:end].strip()
    body = content[end + 3 :].strip()

    # Simple YAML key-value parsing (avoids pyyaml dependency)
    metadata: dict = {}
    for line in frontmatter_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

    return metadata, body


def parse_memory_file(mf: MemoryFile) -> Document:
    """Convert a MemoryFile into a searchable Document.

    Args:
        mf: A scanned memory file.

    Returns:
        A Document with ``doc_id="amem:<slug>/<stem>"``.
    """
    metadata, body = _strip_frontmatter(mf.content)

    # Use frontmatter name as heading if available, else derive from filename
    name = metadata.get("name", "")
    if not name:
        name = mf.path.stem.replace("_", " ").replace("-", " ").title()

    doc_id = f"amem:{mf.project_slug}/{mf.path.stem}"
    heading = f"### {mf.project_slug}: {name}"

    # Include heading + description in content for search indexing
    searchable_parts = [heading]
    if metadata.get("description"):
        searchable_parts.append(metadata["description"])
    searchable_parts.append(body if body else mf.content)
    content = "\n\n".join(searchable_parts)

    return Document(
        doc_id=doc_id,
        source=DocumentSource.AUTO_MEMORY,
        heading=heading,
        content=content,
        metadata={
            "project": mf.project_slug,
            "file_path": str(mf.path),
            "is_index": mf.is_index,
            **{k: v for k, v in metadata.items() if k in ("name", "type", "description")},
        },
    )


def scan_all_auto_memory(claude_projects_dir: Path) -> list[Document]:
    """Scan all Claude Code project memory directories and return Documents.

    Args:
        claude_projects_dir: Path to ``~/.claude/projects/``.

    Returns:
        List of Documents from all projects.
    """
    documents = []
    for memory_dir in find_memory_directories(claude_projects_dir):
        for mf in scan_memory_files(memory_dir):
            documents.append(parse_memory_file(mf))
    return documents


def detect_changes(
    cursor: dict,
    memory_files: list[MemoryFile],
) -> tuple[list[MemoryFile], list[str]]:
    """Compare scanned files against cursor to detect changes.

    Args:
        cursor: The ``"claude-memory"`` section of the cursor dict.
        memory_files: All currently scanned memory files.

    Returns:
        Tuple of (changed_files, deleted_keys) where deleted_keys are
        file paths that existed in the cursor but no longer exist on disk.
    """
    stored_files = cursor.get("files", {})

    changed = []
    current_paths = set()

    for mf in memory_files:
        path_key = str(mf.path)
        current_paths.add(path_key)

        stored = stored_files.get(path_key)
        if stored is None or stored.get("hash") != mf.content_hash:
            changed.append(mf)

    # Detect deleted files
    deleted = [k for k in stored_files if k not in current_paths]

    return changed, deleted


def update_cursor(cursor: dict, memory_files: list[MemoryFile]) -> dict:
    """Update the cursor with current file hashes.

    Args:
        cursor: The full cursor dict (will be modified in place).
        memory_files: All currently scanned memory files.

    Returns:
        The updated cursor dict.
    """
    now = datetime.now(timezone.utc).isoformat()

    files_dict = {}
    for mf in memory_files:
        files_dict[str(mf.path)] = {
            "hash": mf.content_hash,
            "last_seen": now,
        }

    cursor["claude-memory"] = {
        "files": files_dict,
        "last_scan": now,
    }
    return cursor
