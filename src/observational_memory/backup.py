"""Host-local versioned snapshots of memory files (v0.8.0 Gate 1).

This module takes self-contained, byte-faithful snapshots of the durable
Markdown memory files so a bad reflect (or accidental deletion) can be rolled
back. A snapshot is one directory holding the in-scope files plus a
``manifest.json`` with per-file sha256 hashes; it is fully independent and can
be restored on its own.

Scope is deliberately narrow. Snapshots contain ONLY the authoritative Markdown
(``observations.md``, ``reflections.md``, ``profile.md``, ``active.md``). They
never contain ``usage.sqlite``, auth/cluster secrets, ephemeral host state, or
the (rebuildable) search index. See the v0.8.0 plan for the threat model and the
in/out decision. This keeps the bundle pure Markdown and honors the OM Cluster
rule that secrets and host-local binary state must never be persisted into a
portable artifact.

The bundle is NOT automatically safe to copy off-host: ``reflections.md`` can
hold ``scope=local`` entries (and whatever a user typed into their Markdown), so
treat snapshots as host-local. ``backups/`` lives under ``memory_dir`` but is
NOT part of the materialized cluster set, so it is never synced; ``OM_BACKUP_DIR``
must likewise stay host-local (never a synced folder or cluster transport dir).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config
from .sync.atomic import DirectoryLock, atomic_write_bytes, atomic_write_text

SNAPSHOT_FORMAT = "om-memory-snapshot"
SNAPSHOT_FORMAT_VERSION = 1

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_LOCK_TIMEOUT_SECONDS = 5.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# In-scope durable Markdown files: (Config attribute, manifest role).
_MEMORY_FILES: tuple[tuple[str, str], ...] = (
    ("observations_path", "observations"),
    ("reflections_path", "reflections"),
    ("profile_path", "profile"),
    ("active_path", "active"),
)


@dataclass(frozen=True)
class SnapshotInfo:
    """Metadata for one on-disk snapshot directory."""

    snapshot_id: str
    path: Path
    reason: str
    created_at: str
    files: tuple[str, ...]
    bytes_total: int


class RestoreFailedError(RuntimeError):
    """Restore failed mid-write but live memory was rolled back to a safe state."""


class RestorePartialError(RuntimeError):
    """Restore failed AND could not be rolled back — live memory may be mixed.

    Carries the pre-restore safety snapshot id (when one was taken) so the CLI
    can tell the user the exact recovery command.
    """

    def __init__(
        self,
        *,
        snapshot_id: str,
        safety_snapshot_id: str | None,
        original: BaseException,
        rollback_error: BaseException | None,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.safety_snapshot_id = safety_snapshot_id
        self.original = original
        self.rollback_error = rollback_error
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        parts = [
            f"Restore of {self.snapshot_id} failed partway "
            f"({type(self.original).__name__}: {self.original}); "
            "live memory may be in a partially-restored state."
        ]
        if self.safety_snapshot_id is not None:
            parts.append(
                "An automatic rollback was attempted but also failed "
                f"({type(self.rollback_error).__name__}: {self.rollback_error}). "
                f"Recover with: om restore {self.safety_snapshot_id} --no-safety-snapshot"
            )
        else:
            parts.append(
                "No pre-restore safety snapshot was taken (--no-safety-snapshot), "
                "so there is no automatic recovery point."
            )
        return " ".join(parts)


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _om_version() -> str:
    return __version__ or "unknown"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_ORPHAN_TEMP_AGE_SECONDS = 300.0  # reap .tmp-* dirs older than this (crash debris)


def _reap_orphan_temp_dirs(backups_dir: Path) -> None:
    """Best-effort removal of stale ``.tmp-*`` snapshot dirs (held lock assumed).

    A SIGKILL/power loss between temp mkdir and the rename-into-place leaves a
    ``.tmp-`` dir that no other code path ever cleans up. Only reap dirs older
    than a short threshold so we never race a concurrent in-flight snapshot.
    """
    now = datetime.now(timezone.utc).timestamp()
    try:
        children = list(backups_dir.iterdir())
    except OSError:
        return
    for child in children:
        if not child.name.startswith(".tmp-"):
            continue
        try:
            if now - child.stat().st_mtime < _ORPHAN_TEMP_AGE_SECONDS:
                continue
            shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _in_scope_files(config: Config) -> list[tuple[Path, str]]:
    """Return (path, role) for in-scope memory files that currently exist."""
    found: list[tuple[Path, str]] = []
    for attr, role in _MEMORY_FILES:
        path = getattr(config, attr)
        if path.exists():
            found.append((path, role))
    return found


def create_snapshot(
    config: Config,
    reason: str,
    *,
    force: bool = False,
    extra_keep: set[str] | None = None,
) -> SnapshotInfo | None:
    """Copy current in-scope memory files into a new timestamped snapshot dir.

    Returns ``None`` when backups are disabled or there is nothing to snapshot
    (no memory files exist yet). Writes into a temp dir then renames into place,
    so a crash never leaves a half-written snapshot under its final name.
    Applies retention after a successful commit. Raises on real I/O failure —
    callers on hot paths must use :func:`create_snapshot_failclosed`.

    ``force=True`` ignores the ``OM_BACKUP_ENABLED`` toggle. It exists for
    one-shot destructive migrations (e.g. cluster init) whose pre-import safety
    backup must not be defeatable by a global backup/retention switch.

    ``extra_keep`` snapshot ids are pinned during this call's retention pass, so
    a tight ``OM_BACKUP_RETENTION_COUNT`` cannot prune them as a side effect
    (e.g. the snapshot a restore is reading from must survive its own
    pre-restore snapshot's retention).
    """
    if not config.backup_enabled and not force:
        return None

    in_scope = _in_scope_files(config)
    if not in_scope:
        return None

    backups_dir = config.backups_dir
    backups_dir.mkdir(parents=True, exist_ok=True)

    lock = DirectoryLock(backups_dir / ".lock", timeout_seconds=_LOCK_TIMEOUT_SECONDS)
    with lock:
        # Reap orphaned temp dirs from hard-killed/crashed snapshots: list_snapshots
        # and retention both skip .tmp-* dirs, so nothing else cleans them and they
        # grow disk without bound and are invisible to the count/age caps.
        _reap_orphan_temp_dirs(backups_dir)

        snapshot_id = f"{reason}-{_now_timestamp()}"
        final_dir = backups_dir / snapshot_id
        # Avoid clobbering an existing snapshot taken within the same second.
        if final_dir.exists():
            snapshot_id = f"{snapshot_id}-{uuid.uuid4().hex[:6]}"
            final_dir = backups_dir / snapshot_id

        tmp_dir = backups_dir / f".tmp-{uuid.uuid4().hex}"
        tmp_dir.mkdir(parents=True, exist_ok=False)
        try:
            created_at = _now_iso()
            file_entries: list[dict[str, object]] = []
            bytes_total = 0
            for path, role in in_scope:
                data = path.read_bytes()
                atomic_write_bytes(tmp_dir / path.name, data)
                bytes_total += len(data)
                file_entries.append(
                    {
                        "path": path.name,
                        "role": role,
                        "bytes": len(data),
                        "sha256": _sha256_bytes(data),
                    }
                )

            manifest = {
                "format": SNAPSHOT_FORMAT,
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "snapshot_id": snapshot_id,
                "reason": reason,
                "created_at": created_at,
                "om_version": _om_version(),
                "source_memory_dir": str(config.memory_dir),
                "files": file_entries,
            }
            atomic_write_text(tmp_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")

            tmp_dir.replace(final_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        info = SnapshotInfo(
            snapshot_id=snapshot_id,
            path=final_dir,
            reason=reason,
            created_at=created_at,
            files=tuple(entry["path"] for entry in file_entries),  # type: ignore[misc]
            bytes_total=bytes_total,
        )
        _apply_retention_locked(config, keep_id=snapshot_id, extra_keep=extra_keep)
        return info


def create_snapshot_failclosed(config: Config, reason: str) -> SnapshotInfo | None:
    """Never-raises wrapper for hot paths (pre-reflect).

    Logs a one-line diagnostic to stderr on any failure and returns ``None`` so
    a backup hiccup never crashes reflect or loses the new write.
    """
    try:
        return create_snapshot(config, reason)
    except Exception as exc:  # noqa: BLE001 — fail-closed is the whole point
        print(
            f"om: {reason} snapshot failed ({type(exc).__name__}): {exc}; proceeding",
            file=sys.stderr,
        )
        return None


def _read_manifest(snapshot_dir: Path) -> dict | None:
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
        return None
    return data


def _snapshot_info_from_dir(snapshot_dir: Path) -> SnapshotInfo | None:
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        return None
    files = tuple(entry.get("path", "") for entry in manifest.get("files", []) if isinstance(entry, dict))
    bytes_total = sum(int(entry.get("bytes", 0)) for entry in manifest.get("files", []) if isinstance(entry, dict))
    return SnapshotInfo(
        snapshot_id=str(manifest.get("snapshot_id", snapshot_dir.name)),
        path=snapshot_dir,
        reason=str(manifest.get("reason", "unknown")),
        created_at=str(manifest.get("created_at", "")),
        files=files,
        bytes_total=bytes_total,
    )


def list_snapshots(config: Config) -> list[SnapshotInfo]:
    """Return snapshots newest-first. Skips temp and manifest-less dirs."""
    backups_dir = config.backups_dir
    if not backups_dir.exists():
        return []
    snapshots: list[SnapshotInfo] = []
    for child in backups_dir.iterdir():
        if not child.is_dir() or child.name.startswith(".tmp-") or child.name == ".lock":
            continue
        info = _snapshot_info_from_dir(child)
        if info is not None:
            snapshots.append(info)
    snapshots.sort(key=lambda s: (s.created_at, s.snapshot_id), reverse=True)
    return snapshots


def resolve_snapshot(config: Config, selector: str | None) -> SnapshotInfo:
    """Map a selector to a snapshot.

    ``None`` or ``"latest"`` selects the newest; otherwise the ``snapshot_id``
    must match exactly. Raises ``FileNotFoundError``/``ValueError`` with a clear
    message when no match is found.
    """
    snapshots = list_snapshots(config)
    if selector in (None, "latest"):
        if not snapshots:
            raise FileNotFoundError("No snapshots available to restore.")
        return snapshots[0]
    for snapshot in snapshots:
        if snapshot.snapshot_id == selector:
            return snapshot
    raise ValueError(f"No snapshot named {selector!r}. Run `om backup --list` to see available snapshots.")


def restore_snapshot(
    config: Config,
    snapshot: SnapshotInfo,
    *,
    make_safety_snapshot: bool = True,
) -> SnapshotInfo:
    """Byte-faithfully restore in-scope files from ``snapshot`` onto live memory.

    Verifies each file's sha256 against the manifest BEFORE overwriting, so a
    corrupt snapshot aborts without touching live memory. Takes a ``pre-restore``
    safety snapshot of current state first (unless disabled). Returns the safety
    snapshot (or ``snapshot`` itself when the safety snapshot is skipped/empty).

    Phase 2 is transactional across the file SET, not just per file: every new
    body is first staged into a same-directory temp file, and only once ALL
    stages succeed are they atomically replaced into place. If a stage or replace
    still fails partway, the just-taken safety snapshot is restored so live
    memory is never left in a mixed state that existed at no point in time.
    Raises :class:`RestorePartialError` (naming the safety snapshot) only if the
    rollback itself also fails.
    """
    manifest = _read_manifest(snapshot.path)
    if manifest is None:
        raise ValueError(f"Snapshot {snapshot.snapshot_id} has no valid manifest.json.")

    entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict)]
    if not entries:
        raise ValueError(f"Snapshot {snapshot.snapshot_id} lists no files to restore.")

    # Fail closed on a manifest we cannot fully trust BEFORE planning any write:
    # an unknown format version, a duplicate/unknown role, a name that does not
    # match the role's expected basename (tamper / path-traversal guard), or a
    # missing/invalid sha256 or byte count must abort with live memory untouched.
    # Restoring "unchecked bytes" over durable memory would defeat the whole
    # integrity contract.
    version = manifest.get("format_version")
    if version != SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"Snapshot {snapshot.snapshot_id} has unsupported format_version {version!r} "
            f"(expected {SNAPSHOT_FORMAT_VERSION}); refusing to restore."
        )

    role_to_attr = {role: attr for attr, role in _MEMORY_FILES}

    # Phase 1: read + verify every file before writing anything.
    planned: list[tuple[Path, bytes]] = []
    restored_roles: set[str] = set()
    for entry in entries:
        name = entry.get("path")
        role = entry.get("role")
        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if role not in role_to_attr:
            raise ValueError(f"Snapshot {snapshot.snapshot_id} has an unrecognized role: {role!r}.")
        if role in restored_roles:
            raise ValueError(f"Snapshot {snapshot.snapshot_id} lists role {role!r} more than once.")
        target = getattr(config, role_to_attr[role])
        # The stored name must be exactly the role's basename — never another
        # file or a traversal path like '../x'.
        if name != target.name:
            raise ValueError(
                f"Snapshot {snapshot.snapshot_id} maps role {role!r} to unexpected file "
                f"{name!r} (expected {target.name!r})."
            )
        if not isinstance(expected_sha, str) or not _SHA256_RE.match(expected_sha):
            raise ValueError(f"Snapshot {snapshot.snapshot_id} file {name} has no valid sha256; refusing to restore.")
        source = snapshot.path / name
        if not source.exists():
            raise FileNotFoundError(f"Snapshot {snapshot.snapshot_id} is missing {name}.")
        data = source.read_bytes()
        actual_sha = _sha256_bytes(data)
        if actual_sha != expected_sha:
            raise ValueError(
                f"Snapshot {snapshot.snapshot_id} file {name} failed integrity check "
                f"(expected {expected_sha}, got {actual_sha})."
            )
        # bool is an int subclass — exclude it explicitly so a stray True/False
        # byte count cannot pass as valid.
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool):
            raise ValueError(
                f"Snapshot {snapshot.snapshot_id} file {name} has no valid byte count; refusing to restore."
            )
        if expected_bytes != len(data):
            raise ValueError(
                f"Snapshot {snapshot.snapshot_id} file {name} byte-count mismatch "
                f"(manifest {expected_bytes}, actual {len(data)})."
            )
        planned.append((target, data))
        restored_roles.add(role)

    # Subset snapshots (taken before some files existed) must still produce a
    # consistent point-in-time, not a merge: any in-scope live file whose role is
    # absent from the manifest is stale relative to what we are restoring and is
    # removed. profile.md/active.md are DERIVED from reflections.md, so when they
    # are missing from the snapshot we regenerate them from the restored
    # reflections rather than leaving them describing a reflections.md that no
    # longer exists.
    unlisted_targets: list[Path] = []
    for role, attr in role_to_attr.items():
        if role in restored_roles:
            continue
        live_path = getattr(config, attr)
        if live_path.exists():
            unlisted_targets.append(live_path)
    needs_startup_refresh = "reflections" in restored_roles and (
        "profile" not in restored_roles or "active" not in restored_roles
    )

    safety = None
    if make_safety_snapshot:
        # force=True: the pre-restore safety snapshot is part of restore's
        # atomicity, not the background backup feature — it must be taken even
        # when OM_BACKUP_ENABLED=0, or a mid-restore failure has nothing to roll
        # back to. extra_keep pins the snapshot we are restoring FROM so this
        # snapshot's own retention pass cannot prune it (tight retention counts).
        safety = create_snapshot(
            config,
            reason="pre-restore",
            force=True,
            extra_keep={snapshot.snapshot_id},
        )

    # Phase 2: transactional across the whole file SET.
    #
    # Stage every new body into a same-directory temp file first; only after ALL
    # stages succeed do we os.replace them into place (fast, far less likely to
    # fail mid-sequence). If anything fails, roll the SET back from the safety
    # snapshot so we never leave a mixed state. We deliberately do NOT use a
    # plain per-file loop here — that is exactly the non-atomic-across-files bug.
    config.ensure_memory_dir()
    staged: list[tuple[Path, Path]] = []  # (temp, target)
    try:
        for target, data in planned:
            temp = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
            atomic_write_bytes(temp, data)
            staged.append((temp, target))
        for temp, target in staged:
            os.replace(temp, target)
        # Remove in-scope live files not present in the snapshot so the result is
        # the snapshot's point-in-time, not a merge with newer files left behind.
        for stale in unlisted_targets:
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
    except Exception as exc:  # noqa: BLE001 — must clean up + roll back, then re-raise
        for temp, _target in staged:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        # Roll the set back to the pre-restore state so live memory is never
        # left in a mixed/Frankenstein state. The safety snapshot is the only
        # consistent point-in-time we have; restore it WITHOUT taking another
        # safety snapshot (we are already mid-recovery).
        if safety is not None:
            try:
                restore_snapshot(config, safety, make_safety_snapshot=False)
            except Exception as rollback_exc:  # noqa: BLE001
                raise RestorePartialError(
                    snapshot_id=snapshot.snapshot_id,
                    safety_snapshot_id=safety.snapshot_id,
                    original=exc,
                    rollback_error=rollback_exc,
                ) from exc
            # Rollback succeeded: live memory is back to its pre-restore state.
            raise RestoreFailedError(
                f"Restore of {snapshot.snapshot_id} failed ({type(exc).__name__}: {exc}); "
                f"live memory was rolled back to its pre-restore state."
            ) from exc
        # No safety net was taken (caller passed --no-safety-snapshot). Some
        # files may already be replaced; we cannot roll back. Surface loudly.
        raise RestorePartialError(
            snapshot_id=snapshot.snapshot_id,
            safety_snapshot_id=None,
            original=exc,
            rollback_error=None,
        ) from exc

    # Regenerate derived startup files from the restored reflections when the
    # snapshot predated profile.md/active.md, so the restored state is internally
    # consistent (profile/active describe the reflections.md that now exists).
    if needs_startup_refresh:
        try:
            from .startup_memory import refresh_startup_memory

            refresh_startup_memory(config)
        except Exception as exc:  # noqa: BLE001 — best-effort; restore already succeeded
            print(
                f"om: restored reflections but failed to regenerate startup memory "
                f"({type(exc).__name__}): {exc}; run `om context` to rebuild",
                file=sys.stderr,
            )

    return safety or snapshot


def apply_retention(config: Config) -> list[SnapshotInfo]:
    """Prune snapshots beyond the configured count/age. Returns pruned ones."""
    backups_dir = config.backups_dir
    if not backups_dir.exists():
        return []
    lock = DirectoryLock(backups_dir / ".lock", timeout_seconds=_LOCK_TIMEOUT_SECONDS)
    with lock:
        return _apply_retention_locked(config, keep_id=None)


def _apply_retention_locked(
    config: Config, *, keep_id: str | None, extra_keep: set[str] | None = None
) -> list[SnapshotInfo]:
    """Retention body; assumes the backups-dir lock is already held.

    Applies the count cap first, then the age cap. Never deletes the newest
    snapshot, the one just created, or any explicitly pinned ``extra_keep`` id.
    Best-effort: deletion failures are swallowed (a stuck delete must not crash
    reflect)."""
    snapshots = list_snapshots(config)
    if not snapshots:
        return []

    keep: set[str] = set()
    keep.add(snapshots[0].snapshot_id)  # never drop the newest
    if keep_id is not None:
        keep.add(keep_id)
    if extra_keep:
        keep.update(extra_keep)

    to_prune: list[SnapshotInfo] = []

    count = config.backup_retention_count
    survivors = snapshots
    if count and count > 0:
        survivors = snapshots[:count]
        for snapshot in snapshots[count:]:
            if snapshot.snapshot_id not in keep:
                to_prune.append(snapshot)

    days = config.backup_retention_days
    if days and days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        for snapshot in survivors:
            if snapshot.snapshot_id in keep:
                continue
            ts = _snapshot_epoch(snapshot)
            if ts is not None and ts < cutoff:
                to_prune.append(snapshot)

    pruned: list[SnapshotInfo] = []
    seen: set[str] = set()
    for snapshot in to_prune:
        if snapshot.snapshot_id in seen or snapshot.snapshot_id in keep:
            continue
        seen.add(snapshot.snapshot_id)
        try:
            shutil.rmtree(snapshot.path)
            pruned.append(snapshot)
        except OSError as exc:  # noqa: PERF203 — best-effort per-snapshot
            print(
                f"om: failed to prune snapshot {snapshot.snapshot_id} ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
    return pruned


def _snapshot_epoch(snapshot: SnapshotInfo) -> float | None:
    """Best-effort epoch seconds for a snapshot's created_at (falls back to mtime)."""
    if snapshot.created_at:
        try:
            return datetime.fromisoformat(snapshot.created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return snapshot.path.stat().st_mtime
    except OSError:
        return None
