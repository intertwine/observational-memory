"""Context packs: scope-filtered memory bundles shipped over OM Mail.

A pack is a plain JSON payload (the envelope layer encrypts it — packs are
never sent without a shared key, see ``service.send_pack``). Two invariants:

- LEAK GUARD: every packed file passes through
  :func:`filter_reflection_document_for_shareout` — the same Gate-4 resolver
  that guards cluster snapshots — so ``scope=local`` (and any explicit
  non-shareable scope) never leaves the host inside a pack.
- FAIL CLOSED ON OPEN: every file is verified against the SHA256 manifest
  before anything touches disk; one mismatch aborts the whole pack.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from observational_memory.reflection_metadata import filter_reflection_document_for_shareout
from observational_memory.sync.atomic import atomic_write_text
from observational_memory.sync.crypto import sha256_id

if TYPE_CHECKING:
    from observational_memory.config import Config

PACK_FILES = ("profile.md", "active.md", "reflections.md")


class PackError(ValueError):
    """The context pack is empty, malformed, or fails manifest verification."""


def _pack_source_paths(config: Config) -> dict[str, Path]:
    return {
        "profile.md": config.profile_path,
        "active.md": config.active_path,
        "reflections.md": config.reflections_path,
    }


def build_context_pack(
    config: Config,
    *,
    include: tuple[str, ...] = PACK_FILES,
    host_alias: str | None = None,
) -> dict[str, Any]:
    """Collect memory files into a manifest-hashed pack payload.

    Missing files are omitted; files left with no shareable content after the
    scope filter are omitted too. An entirely empty pack raises
    :class:`PackError` rather than mailing a hollow artifact.
    """
    sources = _pack_source_paths(config)
    files: dict[str, str] = {}
    manifest: dict[str, str] = {}
    for filename in include:
        if filename not in sources:
            raise PackError(f"Unknown pack file: {filename!r} (known: {', '.join(PACK_FILES)})")
        path = sources[filename]
        if not path.exists():
            continue
        filtered = filter_reflection_document_for_shareout(path.read_text())
        if not filtered.strip():
            continue
        files[filename] = filtered
        manifest[filename] = sha256_id(filtered.encode("utf-8"))
    if not files:
        raise PackError("Context pack is empty: no shareable memory content found.")
    return {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_alias": host_alias,
        "manifest": manifest,
        "files": files,
    }


def open_context_pack(payload: dict[str, Any], dest_dir: Path) -> list[Path]:
    """Verify a pack payload against its manifest and write it under ``dest_dir``.

    Verification is all-or-nothing: every file's SHA256 must match its
    manifest entry, the manifest and files keys must agree, and filenames must
    be plain basenames — any failure raises :class:`PackError` BEFORE a single
    byte is written.
    """
    if not isinstance(payload, dict):
        raise PackError("Context pack payload must be a JSON object.")
    manifest = payload.get("manifest")
    files = payload.get("files")
    if not isinstance(manifest, dict) or not isinstance(files, dict):
        raise PackError("Context pack payload missing manifest or files.")
    if not files:
        raise PackError("Context pack contains no files.")
    if set(manifest) != set(files):
        raise PackError("Context pack manifest does not match its file set.")
    for filename, text in files.items():
        if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise PackError(f"Context pack filename is not a safe basename: {filename!r}")
        if not isinstance(text, str):
            raise PackError(f"Context pack file is not text: {filename!r}")
        expected = manifest[filename]
        actual = sha256_id(text.encode("utf-8"))
        if actual != expected:
            raise PackError(f"Context pack hash mismatch for {filename!r}; refusing to open.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename in sorted(files):
        target = dest_dir / filename
        atomic_write_text(target, files[filename], mode=0o600)
        written.append(target)
    return written
