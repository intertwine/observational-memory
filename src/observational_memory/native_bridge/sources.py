"""Stable, allowlisted native-memory source capture."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .secure_fs import SecureAccessError, SecureRoot

_TRANSCRIPT_COMPONENTS = frozenset(
    {
        "archived_sessions",
        "session_data",
        "sessions",
        "transcripts",
    }
)
_RAW_MEMORY_NAMES = frozenset({"raw_memories.md"})


@dataclass(frozen=True)
class SourceArtifact:
    source_id: str
    agent: str
    relative_path: str
    content_hash: str
    content: bytes


@dataclass(frozen=True)
class StableSnapshot:
    artifacts: tuple[SourceArtifact, ...]

    @property
    def source_hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple((artifact.source_id, artifact.content_hash) for artifact in self.artifacts)


def _validate_relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SecureAccessError(f"unsafe allowlist path: {value}")
    lowered = {part.lower() for part in path.parts}
    if path.name.lower() in _RAW_MEMORY_NAMES or lowered.intersection(_TRANSCRIPT_COMPONENTS):
        raise SecureAccessError(f"raw memory and transcripts are excluded: {value}")
    if path.suffix.lower() != ".md":
        raise SecureAccessError(f"native-memory inputs must be Markdown: {value}")
    return str(path)


def _artifact(agent: str, relative: str, content: bytes) -> SourceArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return SourceArtifact(
        source_id=f"{agent}:{relative}",
        agent=agent,
        relative_path=relative,
        content_hash=digest,
        content=content,
    )


def capture_codex(
    root_path: Path,
    allowlist: tuple[str, ...],
    *,
    max_file_bytes: int,
) -> list[SourceArtifact]:
    artifacts: list[SourceArtifact] = []
    with SecureRoot(root_path, writable=False) as root:
        for configured in sorted(set(allowlist)):
            relative = _validate_relative_file(configured)
            try:
                content, _info = root.read_bytes(relative, max_bytes=max_file_bytes)
            except FileNotFoundError:
                continue
            artifacts.append(_artifact("codex", relative, content))
    return artifacts


def capture_claude(
    projects_root: Path,
    projects: tuple[str, ...],
    *,
    max_file_bytes: int,
) -> list[SourceArtifact]:
    artifacts: list[SourceArtifact] = []
    with SecureRoot(projects_root, writable=False) as root:
        for project in sorted(set(projects)):
            project_path = PurePosixPath(project)
            if len(project_path.parts) != 1 or project_path.name in {"", ".", ".."}:
                raise SecureAccessError(f"Claude project opt-in must be one directory name: {project}")
            memory_relative = f"{project}/memory"
            for name in root.list_directory(memory_relative):
                if name.lower() in _RAW_MEMORY_NAMES:
                    continue
                if PurePosixPath(name).suffix.lower() != ".md":
                    continue
                relative = _validate_relative_file(f"{memory_relative}/{name}")
                try:
                    content, _info = root.read_bytes(relative, max_bytes=max_file_bytes)
                except FileNotFoundError:
                    raise SecureAccessError(f"Claude input disappeared during snapshot: {relative}") from None
                artifacts.append(_artifact("claude", relative, content))
    return artifacts


def capture_native_snapshot(
    *,
    codex_root: Path,
    codex_allowlist: tuple[str, ...],
    claude_projects_root: Path,
    claude_projects: tuple[str, ...],
    max_file_bytes: int,
    max_total_bytes: int,
) -> StableSnapshot:
    def capture_once() -> StableSnapshot:
        artifacts = capture_codex(codex_root, codex_allowlist, max_file_bytes=max_file_bytes)
        if claude_projects:
            artifacts.extend(capture_claude(claude_projects_root, claude_projects, max_file_bytes=max_file_bytes))
        artifacts.sort(key=lambda artifact: artifact.source_id)
        total = sum(len(artifact.content) for artifact in artifacts)
        if total > max_total_bytes:
            raise SecureAccessError(f"native-memory snapshot exceeds {max_total_bytes} byte limit")
        return StableSnapshot(tuple(artifacts))

    previous = capture_once()
    for _attempt in range(3):
        current = capture_once()
        if current.source_hashes == previous.source_hashes:
            return current
        previous = current
    raise SecureAccessError("native-memory sources did not produce two identical stable snapshots")
