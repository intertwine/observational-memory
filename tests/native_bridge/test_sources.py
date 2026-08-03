from __future__ import annotations

import pytest

from observational_memory.native_bridge.secure_fs import SecureAccessError
from observational_memory.native_bridge.sources import (
    SourceArtifact,
    capture_claude,
    capture_codex,
    capture_native_snapshot,
)


@pytest.mark.parametrize("component", ["project", "memory", "leaf"])
def test_claude_symlink_in_each_source_component_is_rejected(tmp_path, component):
    projects = tmp_path / "projects"
    outside = tmp_path / "outside"
    projects.mkdir()
    outside.mkdir()
    (outside / "MEMORY.md").write_text("outside")
    project = projects / "project-a"
    if component == "project":
        project.symlink_to(outside, target_is_directory=True)
    else:
        project.mkdir()
        if component == "memory":
            (project / "memory").symlink_to(outside, target_is_directory=True)
        else:
            memory = project / "memory"
            memory.mkdir()
            (memory / "MEMORY.md").symlink_to(outside / "MEMORY.md")

    with pytest.raises(SecureAccessError):
        capture_claude(projects, ("project-a",), max_file_bytes=1024)


def test_codex_symlink_leaf_is_rejected(tmp_path):
    memories = tmp_path / "memories"
    outside = tmp_path / "outside.md"
    memories.mkdir()
    outside.write_text("outside")
    (memories / "MEMORY.md").symlink_to(outside)

    with pytest.raises(SecureAccessError):
        capture_codex(memories, ("MEMORY.md",), max_file_bytes=1024)


def test_claude_raw_memory_is_excluded_not_read(tmp_path):
    projects = tmp_path / "projects"
    memory = projects / "project-a" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("approved")
    raw_target = tmp_path / "raw-target.md"
    raw_target.write_text("raw")
    (memory / "raw_memories.md").symlink_to(raw_target)

    artifacts = capture_claude(projects, ("project-a",), max_file_bytes=1024)

    assert [artifact.relative_path for artifact in artifacts] == ["project-a/memory/MEMORY.md"]


@pytest.mark.parametrize("project", ["project\nname", "project\tname", "project\x7fname"])
def test_claude_capture_rejects_control_characters_in_project_name(tmp_path, project):
    projects = tmp_path / "projects"
    projects.mkdir()

    with pytest.raises(SecureAccessError, match="one directory name"):
        capture_claude(projects, (project,), max_file_bytes=1024)


def test_snapshot_requires_two_identical_complete_captures(tmp_path, monkeypatch):
    import observational_memory.native_bridge.sources as sources

    calls = 0

    def changing_codex(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        content = f"version-{calls}".encode()
        return [
            SourceArtifact(
                source_id="codex:MEMORY.md",
                agent="codex",
                relative_path="MEMORY.md",
                content_hash=str(calls),
                content=content,
            )
        ]

    monkeypatch.setattr(sources, "capture_codex", changing_codex)

    with pytest.raises(SecureAccessError, match="two identical"):
        capture_native_snapshot(
            codex_root=tmp_path,
            codex_allowlist=("MEMORY.md",),
            claude_projects_root=tmp_path,
            claude_projects=(),
            max_file_bytes=1024,
            max_total_bytes=1024,
        )
