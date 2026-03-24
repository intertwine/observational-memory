"""Tests for Claude Code auto-memory scanner and integration."""

from __future__ import annotations

from pathlib import Path

from observational_memory.config import Config
from observational_memory.search import DocumentSource, reindex
from observational_memory.transcripts.auto_memory import (
    MemoryFile,
    _content_hash,
    _strip_frontmatter,
    detect_changes,
    extract_project_slug,
    find_memory_directories,
    parse_memory_file,
    scan_all_auto_memory,
    scan_memory_files,
    update_cursor,
)

# --- Fixtures ---


def _make_project(projects_dir: Path, name: str, files: dict[str, str]) -> Path:
    """Create a fake Claude Code project memory directory."""
    memory_dir = projects_dir / name / "memory"
    memory_dir.mkdir(parents=True)
    for filename, content in files.items():
        (memory_dir / filename).write_text(content)
    return memory_dir


SAMPLE_MEMORY_MD = """\
# Memory Index

- [feedback_scoring.md](feedback_scoring.md) — Scoring feedback
"""

SAMPLE_FEEDBACK_MD = """\
---
name: Dense retrieval scoring
description: Hybrid search must not override FTS ranking
type: feedback
---

Dense scoring bonuses must be tiebreakers, not primary signals.
"""

SAMPLE_PLAIN_MD = """\
# Architecture

The system uses a plugin-based backend.
"""


# --- Project Slug Extraction ---


class TestProjectSlugExtraction:
    def test_standard_path(self):
        assert (
            extract_project_slug("-Users-bryanyoung-experiments-hive-orchestrator") == "experiments-hive-orchestrator"
        )

    def test_short_path(self):
        # Only "Users" + username — not enough parts to strip, keeps as-is
        assert extract_project_slug("-Users-bryanyoung") == "Users-bryanyoung"

    def test_single_segment(self):
        assert extract_project_slug("-myproject") == "myproject"

    def test_empty_segments(self):
        result = extract_project_slug("---")
        assert result == "unknown" or result  # should not crash

    def test_deep_path_preserves_uniqueness(self):
        """Two projects with the same leaf but different parents must not collide."""
        slug_a = extract_project_slug("-Users-bryanyoung-work-client-portal")
        slug_b = extract_project_slug("-Users-bryanyoung-repos-client-portal")
        assert slug_a == "work-client-portal"
        assert slug_b == "repos-client-portal"
        assert slug_a != slug_b

    def test_non_users_prefix(self):
        """Paths not starting with Users keep all segments."""
        assert extract_project_slug("-opt-projects-myapp") == "opt-projects-myapp"


# --- Find Memory Directories ---


class TestFindMemoryDirectories:
    def test_finds_directories_with_files(self, tmp_path):
        _make_project(tmp_path, "project-a", {"MEMORY.md": "# Index"})
        _make_project(tmp_path, "project-b", {"MEMORY.md": "# Index", "notes.md": "Notes"})

        dirs = find_memory_directories(tmp_path)
        assert len(dirs) == 2

    def test_skips_empty_directories(self, tmp_path):
        _make_project(tmp_path, "project-a", {"MEMORY.md": "# Index"})
        # Create empty memory dir
        (tmp_path / "project-b" / "memory").mkdir(parents=True)

        dirs = find_memory_directories(tmp_path)
        assert len(dirs) == 1

    def test_nonexistent_dir(self, tmp_path):
        dirs = find_memory_directories(tmp_path / "nonexistent")
        assert dirs == []


# --- Scan Memory Files ---


class TestScanMemoryFiles:
    def test_scans_all_md_files(self, tmp_path):
        mem_dir = _make_project(
            tmp_path,
            "project-a",
            {
                "MEMORY.md": SAMPLE_MEMORY_MD,
                "feedback_scoring.md": SAMPLE_FEEDBACK_MD,
            },
        )

        files = scan_memory_files(mem_dir)
        assert len(files) == 2

    def test_identifies_index_file(self, tmp_path):
        mem_dir = _make_project(
            tmp_path,
            "project-a",
            {
                "MEMORY.md": SAMPLE_MEMORY_MD,
                "notes.md": SAMPLE_PLAIN_MD,
            },
        )

        files = scan_memory_files(mem_dir)
        index_files = [f for f in files if f.is_index]
        assert len(index_files) == 1
        assert index_files[0].path.name == "MEMORY.md"

    def test_computes_content_hash(self, tmp_path):
        mem_dir = _make_project(tmp_path, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        files = scan_memory_files(mem_dir)
        assert files[0].content_hash == _content_hash(SAMPLE_MEMORY_MD)

    def test_skips_empty_files(self, tmp_path):
        mem_dir = _make_project(
            tmp_path,
            "project-a",
            {
                "MEMORY.md": SAMPLE_MEMORY_MD,
                "empty.md": "",
                "whitespace.md": "   \n  \n  ",
            },
        )

        files = scan_memory_files(mem_dir)
        assert len(files) == 1  # only MEMORY.md

    def test_extracts_project_slug(self, tmp_path):
        mem_dir = _make_project(
            tmp_path,
            "-Users-bryanyoung-experiments-hive-orchestrator",
            {
                "MEMORY.md": SAMPLE_MEMORY_MD,
            },
        )

        files = scan_memory_files(mem_dir)
        assert files[0].project_slug == "experiments-hive-orchestrator"


# --- Frontmatter Stripping ---


class TestStripFrontmatter:
    def test_with_frontmatter(self):
        metadata, body = _strip_frontmatter(SAMPLE_FEEDBACK_MD)
        assert metadata["name"] == "Dense retrieval scoring"
        assert metadata["type"] == "feedback"
        assert "Dense scoring bonuses" in body
        assert "---" not in body

    def test_without_frontmatter(self):
        metadata, body = _strip_frontmatter(SAMPLE_PLAIN_MD)
        assert metadata == {}
        assert body == SAMPLE_PLAIN_MD

    def test_empty_content(self):
        metadata, body = _strip_frontmatter("")
        assert metadata == {}
        assert body == ""


# --- Parse Memory File ---


class TestParseMemoryFile:
    def test_creates_document_with_correct_id(self, tmp_path):
        mem_dir = _make_project(tmp_path, "project-a", {"feedback.md": SAMPLE_FEEDBACK_MD})
        files = scan_memory_files(mem_dir)
        doc = parse_memory_file(files[0])

        assert doc.doc_id == "amem:project-a/feedback"
        assert doc.source == DocumentSource.AUTO_MEMORY

    def test_extracts_frontmatter_metadata(self, tmp_path):
        mem_dir = _make_project(tmp_path, "project-a", {"feedback.md": SAMPLE_FEEDBACK_MD})
        files = scan_memory_files(mem_dir)
        doc = parse_memory_file(files[0])

        assert doc.metadata["name"] == "Dense retrieval scoring"
        assert doc.metadata["type"] == "feedback"
        assert doc.metadata["project"] == "project-a"

    def test_plain_markdown_uses_filename_as_name(self, tmp_path):
        mem_dir = _make_project(tmp_path, "project-a", {"architecture.md": SAMPLE_PLAIN_MD})
        files = scan_memory_files(mem_dir)
        doc = parse_memory_file(files[0])

        assert "Architecture" in doc.heading


# --- Change Detection ---


class TestChangeDetection:
    def test_all_new_files(self):
        files = [
            MemoryFile(Path("/a.md"), "proj", "content", "hash1", False),
        ]
        changed, deleted = detect_changes({}, files)
        assert len(changed) == 1
        assert deleted == []

    def test_unchanged_files(self):
        files = [
            MemoryFile(Path("/a.md"), "proj", "content", "hash1", False),
        ]
        cursor = {"files": {"/a.md": {"hash": "hash1", "last_seen": "now"}}}
        changed, deleted = detect_changes(cursor, files)
        assert changed == []
        assert deleted == []

    def test_modified_file(self):
        files = [
            MemoryFile(Path("/a.md"), "proj", "new content", "hash2", False),
        ]
        cursor = {"files": {"/a.md": {"hash": "hash1", "last_seen": "now"}}}
        changed, deleted = detect_changes(cursor, files)
        assert len(changed) == 1
        assert deleted == []

    def test_deleted_file(self):
        files = []
        cursor = {"files": {"/gone.md": {"hash": "hash1", "last_seen": "now"}}}
        changed, deleted = detect_changes(cursor, files)
        assert changed == []
        assert len(deleted) == 1
        assert deleted[0] == "/gone.md"

    def test_mixed_changes(self):
        files = [
            MemoryFile(Path("/a.md"), "proj", "same", "hash1", False),
            MemoryFile(Path("/b.md"), "proj", "modified", "hash3", False),
            MemoryFile(Path("/c.md"), "proj", "new", "hash4", False),
        ]
        cursor = {
            "files": {
                "/a.md": {"hash": "hash1", "last_seen": "now"},
                "/b.md": {"hash": "hash2", "last_seen": "now"},
                "/deleted.md": {"hash": "hash5", "last_seen": "now"},
            }
        }
        changed, deleted = detect_changes(cursor, files)
        assert len(changed) == 2  # b modified + c new
        assert deleted == ["/deleted.md"]


# --- Cursor Update ---


class TestUpdateCursor:
    def test_updates_cursor_structure(self):
        files = [
            MemoryFile(Path("/a.md"), "proj", "content", "hash1", False),
        ]
        cursor = {}
        update_cursor(cursor, files)

        assert "claude-memory" in cursor
        assert "/a.md" in cursor["claude-memory"]["files"]
        assert cursor["claude-memory"]["files"]["/a.md"]["hash"] == "hash1"
        assert "last_scan" in cursor["claude-memory"]


# --- Scan All ---


class TestScanAllAutoMemory:
    def test_returns_documents_from_multiple_projects(self, tmp_path):
        _make_project(tmp_path, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})
        _make_project(tmp_path, "project-b", {"notes.md": SAMPLE_PLAIN_MD})

        docs = scan_all_auto_memory(tmp_path)
        assert len(docs) == 2

        projects = {d.metadata["project"] for d in docs}
        assert "project-a" in projects
        assert "project-b" in projects


# --- Reindex Integration ---


class TestReindexWithAutoMemory:
    def test_reindex_includes_auto_memory(self, tmp_path):
        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"feedback.md": SAMPLE_FEEDBACK_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        n = reindex(config)
        assert n == 1  # one auto-memory document

    def test_auto_memory_in_index(self, tmp_path):
        """Verify auto-memory documents are included in the BM25 index."""
        from observational_memory.search import get_backend

        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"feedback.md": SAMPLE_FEEDBACK_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        reindex(config)

        backend = get_backend("bm25", config)
        assert backend.is_ready()
        amem_docs = [d for d in backend._documents if d.source == DocumentSource.AUTO_MEMORY]
        assert len(amem_docs) == 1
        assert amem_docs[0].doc_id == "amem:project-a/feedback"
        assert "Dense retrieval scoring" in amem_docs[0].content


# --- Observe Auto Memory ---


class TestObserveAutoMemory:
    def test_detects_new_files(self, tmp_path):
        from observational_memory.observe import observe_auto_memory

        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        changed, deleted = observe_auto_memory(config)
        assert len(changed) == 1
        assert deleted == []

    def test_no_changes_on_second_run(self, tmp_path):
        from observational_memory.observe import observe_auto_memory

        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        observe_auto_memory(config)  # first run
        changed, deleted = observe_auto_memory(config)  # second run
        assert changed == []
        assert deleted == []

    def test_detects_modified_file(self, tmp_path):
        from observational_memory.observe import observe_auto_memory

        projects_dir = tmp_path / "projects"
        mem_dir = _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        observe_auto_memory(config)  # first run

        # Modify the file
        (mem_dir / "MEMORY.md").write_text("# Updated Index\n\nNew content.")
        changed, _deleted = observe_auto_memory(config)
        assert len(changed) == 1

    def test_dry_run_does_not_update_cursor(self, tmp_path):
        from observational_memory.observe import observe_auto_memory

        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        changed, _deleted = observe_auto_memory(config, dry_run=True)
        assert len(changed) == 1

        # Cursor should not have been updated
        cursor = config.load_cursor()
        assert "claude-memory" not in cursor

    def test_no_llm_calls(self, tmp_path, monkeypatch):
        """Verify observe_auto_memory never calls the LLM."""
        from observational_memory import observe

        projects_dir = tmp_path / "projects"
        _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="none", claude_projects_dir=projects_dir)

        # Monkey-patch the compress function to fail if called
        def _fail(*args, **kwargs):
            raise AssertionError("LLM compress() should never be called for auto-memory")

        monkeypatch.setattr(observe, "compress", _fail)
        changed, _deleted = observe.observe_auto_memory(config)
        assert len(changed) == 1

    def test_deleted_last_file_clears_stale_cursor(self, tmp_path):
        """Deleting the last auto-memory file must clear cursor and trigger reindex."""
        from observational_memory.observe import observe_auto_memory

        projects_dir = tmp_path / "projects"
        mem_dir = _make_project(projects_dir, "project-a", {"MEMORY.md": SAMPLE_MEMORY_MD})

        config = Config(memory_dir=tmp_path, search_backend="bm25", claude_projects_dir=projects_dir)
        observe_auto_memory(config)  # first run — tracks file

        cursor = config.load_cursor()
        assert len(cursor["claude-memory"]["files"]) == 1

        # Delete the file
        (mem_dir / "MEMORY.md").unlink()

        # Second run — must detect deletion and report it
        changed, deleted = observe_auto_memory(config)
        assert changed == []
        assert len(deleted) == 1
        # Cursor should now track zero files
        cursor = config.load_cursor()
        assert len(cursor["claude-memory"]["files"]) == 0
