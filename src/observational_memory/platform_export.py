"""Export local OM memory into platform-native seed bundles."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .startup_memory import ensure_startup_memory

PLATFORM_EXPORT_TARGETS = ("generic", "chatgpt", "claude-managed-agents")

_MAX_CLAUDE_MEMORY_BYTES = 95_000


@dataclass(frozen=True)
class ExportedFile:
    """A generated file inside an export bundle."""

    path: Path
    role: str


@dataclass(frozen=True)
class PlatformExportResult:
    """Result metadata for a platform memory export."""

    output_dir: Path
    target: str
    files: tuple[ExportedFile, ...]


def export_platform_memory(
    config: Config | None = None,
    *,
    target: str = "generic",
    output_dir: Path | None = None,
    include_observations: bool = False,
    overwrite: bool = False,
    generated_at: datetime | None = None,
) -> PlatformExportResult:
    """Export OM memory into a local bundle for another memory system.

    The export is intentionally file-based. Current ChatGPT memory controls are
    user-mediated, and Claude Managed Agents memory stores can be seeded with
    text documents. This keeps OM as the local source of truth while producing
    reviewable artifacts for platform-native memory systems.
    """
    if config is None:
        config = Config()

    target = _normalize_target(target)
    generated_at = generated_at or datetime.now(timezone.utc)
    output_dir = output_dir or _default_output_dir(config, target, generated_at)
    output_dir = output_dir.expanduser()

    _guard_memory_dir(config.memory_dir, output_dir)
    _prepare_output_dir(output_dir, overwrite=overwrite)
    ensure_startup_memory(config)
    sources = _read_memory_sources(config)

    if target == "chatgpt":
        files = _export_chatgpt(output_dir, sources, include_observations)
    elif target == "claude-managed-agents":
        files = _export_claude_managed_agents(output_dir, sources, include_observations)
    else:
        files = _export_generic(output_dir, sources, include_observations)

    manifest = _write_manifest(output_dir, target, config, generated_at, files)
    files.append(manifest)

    return PlatformExportResult(output_dir=output_dir, target=target, files=tuple(files))


def _normalize_target(target: str) -> str:
    normalized = target.strip().lower()
    if normalized == "claude":
        normalized = "claude-managed-agents"
    if normalized not in PLATFORM_EXPORT_TARGETS:
        choices = ", ".join(PLATFORM_EXPORT_TARGETS)
        raise ValueError(f"Unknown export target {target!r}; use one of: {choices}.")
    return normalized


def _default_output_dir(config: Config, target: str, generated_at: datetime) -> Path:
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return config.memory_dir / "exports" / f"{target}-{stamp}"


def _prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"{output_dir} already exists and is not empty. Pass --overwrite to replace it.")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _guard_memory_dir(memory_dir: Path, output_dir: Path) -> None:
    """Prevent export overwrite operations from targeting the OM memory root."""
    memory_root = memory_dir.expanduser().resolve(strict=False)
    output_root = output_dir.expanduser().resolve(strict=False)
    if output_root == memory_root or _is_parent(output_root, memory_root):
        raise ValueError(f"Refusing to use {output_dir} because it would contain the OM source memory directory.")


def _is_parent(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_memory_sources(config: Config) -> dict[str, str]:
    return {
        "profile": _read_text(config.profile_path),
        "active": _read_text(config.active_path),
        "reflections": _read_text(config.reflections_path),
        "observations": _read_text(config.observations_path),
    }


def _read_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _export_chatgpt(output_dir: Path, sources: dict[str, str], include_observations: bool) -> list[ExportedFile]:
    readme = """# ChatGPT memory export

This bundle is for user-mediated ChatGPT memory and project context workflows.
ChatGPT Memory currently works through ChatGPT's personalization controls, not a
general developer write API. Review this seed before pasting it into a chat,
adding it to a project, or uploading it as a reference file.

Suggested use:

1. Open ChatGPT in the project or workspace that should use this context.
2. Paste or upload `chatgpt-memory-seed.md`.
3. Ask ChatGPT to remember only the stable facts and preferences you approve.
4. Keep Observational Memory as the local source of truth and regenerate this
   bundle when you want to refresh platform-native context.
"""

    seed_parts = [
        "# ChatGPT memory seed",
        "",
        "Use this as a reviewed memory seed from Observational Memory.",
        "",
        "Guidance for ChatGPT:",
        "",
        "- Prefer high-level preferences, durable facts, and active project context.",
        "- Do not treat this as an exact template store or a verbatim knowledge base.",
        "- Ask before saving sensitive health, financial, legal, credential, or private third-party information.",
        "- If any item looks stale or contradictory, ask the user before relying on it.",
        "",
    ]
    if sources["profile"].strip():
        seed_parts.extend(["## Stable profile", "", sources["profile"].strip(), ""])
    if sources["active"].strip():
        seed_parts.extend(["## Active context", "", sources["active"].strip(), ""])
    if include_observations and sources["observations"].strip():
        seed_parts.extend(
            [
                "## Recent observations",
                "",
                "These are recent and may be transient. Use them for orientation, not permanent saved memories.",
                "",
                sources["observations"].strip(),
                "",
            ]
        )

    files = [
        _write_file(output_dir / "README.md", readme, "instructions"),
        _write_file(output_dir / "chatgpt-memory-seed.md", "\n".join(seed_parts).rstrip() + "\n", "memory_seed"),
    ]
    return files


def _export_claude_managed_agents(
    output_dir: Path,
    sources: dict[str, str],
    include_observations: bool,
) -> list[ExportedFile]:
    readme = """# Claude Managed Agents memory export

This bundle is structured for Claude Managed Agents memory stores. Import the
files under `memories/` into a memory store, or seed equivalent paths through
the Claude Platform API/Console.

Recommended access pattern:

- Attach these imported OM files as read-only reference memory unless the agent
  has a specific reason to modify them.
- Keep agent-authored learnings in a separate read-write memory store.
- Review dream-generated memory changes before treating them as durable if the
  agent processes untrusted prompts, fetched web pages, or third-party tool output.

Observational Memory remains the local source of truth. Regenerate this export
when you want to refresh Claude's platform-native memory from local OM state.
"""

    files = [_write_file(output_dir / "README.md", readme, "instructions")]
    memories_dir = output_dir / "memories"

    if sources["profile"].strip():
        files.extend(_write_chunked_file(memories_dir / "profile.md", sources["profile"].strip(), "profile"))
    if sources["active"].strip():
        files.extend(_write_chunked_file(memories_dir / "active-context.md", sources["active"].strip(), "active"))

    for heading, body in _split_h2_sections(sources["reflections"]):
        if not body.strip():
            continue
        stem = _slugify_heading(heading)
        files.extend(_write_chunked_file(memories_dir / "reflections" / f"{stem}.md", body.strip(), "reflection"))

    if include_observations and sources["observations"].strip():
        files.extend(
            _write_chunked_file(
                memories_dir / "recent-observations.md",
                sources["observations"].strip(),
                "recent_observations",
            )
        )

    return files


def _export_generic(output_dir: Path, sources: dict[str, str], include_observations: bool) -> list[ExportedFile]:
    readme = """# Observational Memory export

This generic bundle contains reviewable local markdown memory for systems that
can consume files, project instructions, or uploaded references.
"""

    files = [_write_file(output_dir / "README.md", readme, "instructions")]
    for key, role in (("profile", "profile"), ("active", "active"), ("reflections", "reflections")):
        if sources[key].strip():
            files.append(_write_file(output_dir / f"{key}.md", sources[key].strip() + "\n", role))
    if include_observations and sources["observations"].strip():
        files.append(
            _write_file(output_dir / "observations.md", sources["observations"].strip() + "\n", "observations")
        )
    return files


def _write_manifest(
    output_dir: Path,
    target: str,
    config: Config,
    generated_at: datetime,
    files: list[ExportedFile],
) -> ExportedFile:
    manifest = {
        "target": target,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "source_memory_dir": str(config.memory_dir),
        "files": [
            {
                "path": str(file.path.relative_to(output_dir)),
                "role": file.role,
                "bytes": file.path.stat().st_size,
            }
            for file in files
        ],
    }
    return _write_file(output_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n", "manifest")


def _write_file(path: Path, content: str, role: str) -> ExportedFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return ExportedFile(path=path, role=role)


def _write_chunked_file(path: Path, content: str, role: str) -> list[ExportedFile]:
    chunks = _chunk_text(content, max_bytes=_MAX_CLAUDE_MEMORY_BYTES)
    if len(chunks) == 1:
        return [_write_file(path, chunks[0].rstrip() + "\n", role)]

    files = []
    suffix = path.suffix or ".md"
    stem = path.with_suffix("")
    for index, chunk in enumerate(chunks, 1):
        chunk_path = Path(f"{stem}-part-{index:02d}{suffix}")
        files.append(_write_file(chunk_path, chunk.rstrip() + "\n", role))
    return files


def _chunk_text(content: str, *, max_bytes: int) -> list[str]:
    if len(content.encode()) <= max_bytes:
        return [content]

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for line in content.splitlines():
        line_with_newline = line + "\n"
        line_bytes = len(line_with_newline.encode())
        if current and current_bytes + line_bytes > max_bytes:
            chunks.append("".join(current).rstrip())
            current = []
            current_bytes = 0
        if line_bytes > max_bytes:
            chunks.extend(_split_long_line(line_with_newline, max_bytes=max_bytes))
            continue
        current.append(line_with_newline)
        current_bytes += line_bytes

    if current:
        chunks.append("".join(current).rstrip())
    return chunks or [content]


def _split_long_line(line: str, *, max_bytes: int) -> list[str]:
    pieces = []
    current = ""
    for char in line:
        if current and len((current + char).encode()) > max_bytes:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def _split_h2_sections(markdown: str) -> list[tuple[str, str]]:
    if not markdown.strip():
        return []

    matches = list(re.finditer(r"^## .+$", markdown, flags=re.MULTILINE))
    if not matches:
        return [("Reflections", markdown)]

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        section = markdown[start:end].strip()
        heading = match.group(0).removeprefix("## ").strip()
        sections.append((heading, section))
    return sections


def _slugify_heading(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"
