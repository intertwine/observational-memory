"""Descriptor-anchored file access for every native-memory bridge path."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


class SecureAccessError(RuntimeError):
    """Raised when a bridge path violates the secure file policy."""


class UnstableReadError(SecureAccessError):
    """Raised when a source changes during an exact-size read."""


class SecureTreeError(SecureAccessError):
    """Base class for a failed read-only tree snapshot."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SecureTreeStructuralError(SecureTreeError):
    """Raised for a persistent policy or topology violation."""


class SecureTreeRetryableError(SecureTreeError):
    """Raised when one later scheduled claim may repeat the snapshot."""


class SecureTreeCapacityError(SecureTreeError):
    """Raised when a stable tree exceeds a reviewed fixed ceiling."""

    def __init__(self, code: str, *, metric: str, observed: int, limit: int) -> None:
        super().__init__(code)
        self.metric = metric
        self.observed = observed
        self.limit = limit


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise SecureAccessError("native-memory bridge requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC")
    if not hasattr(fcntl, "F_GETFD") or not hasattr(fcntl, "FD_CLOEXEC"):
        raise SecureAccessError("native-memory bridge cannot verify close-on-exec")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _verify_close_on_exec(fd: int) -> None:
    try:
        descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError as exc:
        raise SecureAccessError("native-memory bridge cannot verify close-on-exec") from exc
    if descriptor_flags & fcntl.FD_CLOEXEC == 0:
        raise SecureAccessError("native-memory bridge descriptor is not close-on-exec")


def _parts(relative: str | Path) -> tuple[str, ...]:
    value = PurePosixPath(str(relative))
    if value.is_absolute() or not value.parts:
        raise SecureAccessError(f"path must be relative: {relative}")
    if any(part in {"", ".", ".."} for part in value.parts):
        raise SecureAccessError(f"unsafe path component: {relative}")
    return value.parts


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _snapshot_identity(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), int(getattr(info, "st_gen", 0) or 0))


def _snapshot_metadata(info: os.stat_result) -> dict[str, int]:
    result = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "owner": info.st_uid,
        "group": info.st_gid,
        "links": info.st_nlink,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }
    generation = int(getattr(info, "st_gen", 0) or 0)
    if generation:
        result["generation"] = generation
    return result


def _canonical_snapshot_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _file_open_flags() -> int:
    required = ("O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise SecureTreeStructuralError("snapshot-platform-unsupported")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


@dataclass(frozen=True)
class SecureTreeLimits:
    """Fixed capacity bounds for one selected input root."""

    max_depth: int
    max_descendants: int
    max_total_bytes: int
    max_file_bytes: int


@dataclass(frozen=True)
class SecureTreeResult:
    """Canonical products of one complete descriptor-anchored pass."""

    stability_token: str
    semantic_digest: str | None
    descendants: int
    declared_bytes: int


@dataclass(frozen=True)
class SecureTreeGroupResult:
    """Two equal complete observations of the three protected roots."""

    data: SecureTreeResult
    config: SecureTreeResult
    qmd: SecureTreeResult


class SecureTreeSnapshot:
    """Read-only descriptor authority for one complete tree snapshot."""

    _MAX_NAME_BYTES = 255
    _MAX_RELATIVE_PATH_BYTES = 4096
    _READ_CHUNK_BYTES = 128 * 1024

    def __init__(
        self,
        path: Path,
        *,
        limits: SecureTreeLimits,
        content_hash: bool,
        detect_materialize_lock: bool,
    ) -> None:
        selected = Path(path)
        if not selected.is_absolute():
            raise SecureTreeStructuralError("snapshot-root-not-absolute")
        self.path = selected
        self.limits = limits
        self.content_hash = content_hash
        self.detect_materialize_lock = detect_materialize_lock
        self._fd: int | None = self._open_absolute_root()
        try:
            self._checked_directory(
                self._fstat(self._fd),
                root_device=None,
                selected_root=True,
            )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SecureTreeSnapshot:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _raise_retryable(code: str, exc: BaseException) -> None:
        raise SecureTreeRetryableError(code) from exc

    @classmethod
    def _close_descriptor(cls, descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError as exc:
            cls._raise_retryable("snapshot-close-failed", exc)

    @staticmethod
    def _validate_component(component: str, *, code: str) -> bytes:
        if not isinstance(component, str) or component in {"", ".", ".."} or "/" in component or "\x00" in component:
            raise SecureTreeStructuralError(code)
        try:
            encoded = component.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise SecureTreeStructuralError(code) from exc
        if len(encoded) > SecureTreeSnapshot._MAX_NAME_BYTES:
            raise SecureTreeCapacityError(
                "snapshot-name-limit",
                metric="name_bytes",
                observed=len(encoded),
                limit=SecureTreeSnapshot._MAX_NAME_BYTES,
            )
        return encoded

    def _absolute_parts(self) -> tuple[str, ...]:
        raw = os.fspath(self.path)
        if not raw.startswith("/"):
            raise SecureTreeStructuralError("snapshot-root-not-absolute")
        parts = tuple(raw.split("/")[1:])
        if not parts or any(part == "" for part in parts):
            raise SecureTreeStructuralError("snapshot-root-invalid")
        for part in parts:
            self._validate_component(part, code="snapshot-root-invalid")
        return parts

    def _open_absolute_root(self) -> int:
        parts = self._absolute_parts()
        flags = _directory_open_flags()
        try:
            current = os.open("/", flags)
            _verify_close_on_exec(current)
        except (OSError, SecureAccessError) as exc:
            raise SecureTreeRetryableError("snapshot-root-open-failed") from exc
        try:
            for index, component in enumerate(parts):
                child: int | None = None
                try:
                    child = os.open(component, flags, dir_fd=current)
                    _verify_close_on_exec(child)
                    info = os.fstat(child)
                except OSError as exc:
                    if child is not None:
                        self._close_descriptor(child)
                    raise SecureTreeStructuralError("snapshot-root-open-failed") from exc
                except SecureAccessError as exc:
                    if child is not None:
                        self._close_descriptor(child)
                    raise SecureTreeStructuralError("snapshot-close-on-exec-failed") from exc
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
                    self._close_descriptor(child)
                    raise SecureTreeStructuralError("snapshot-root-component-invalid")
                if index == len(parts) - 1 and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
                    self._close_descriptor(child)
                    raise SecureTreeStructuralError("snapshot-root-policy-invalid")
                previous = current
                current = child
                self._close_descriptor(previous)
            return current
        except BaseException:
            self._close_descriptor(current)
            raise

    def close(self) -> None:
        descriptor = getattr(self, "_fd", None)
        if descriptor is not None:
            self._fd = None
            self._close_descriptor(descriptor)

    def _require_open(self) -> int:
        if self._fd is None:
            raise SecureTreeStructuralError("snapshot-root-closed")
        return self._fd

    @staticmethod
    def _checked_directory(
        info: os.stat_result,
        *,
        root_device: int | None,
        selected_root: bool = False,
    ) -> os.stat_result:
        if not stat.S_ISDIR(info.st_mode):
            raise SecureTreeStructuralError("snapshot-special-entry")
        if info.st_uid != os.getuid():
            raise SecureTreeStructuralError("snapshot-owner-invalid")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise SecureTreeStructuralError("snapshot-mode-invalid")
        if not selected_root and root_device is not None and info.st_dev != root_device:
            raise SecureTreeStructuralError("snapshot-mount-crossing")
        return info

    @staticmethod
    def _checked_regular(info: os.stat_result, *, root_device: int) -> os.stat_result:
        if not stat.S_ISREG(info.st_mode):
            raise SecureTreeStructuralError("snapshot-special-entry")
        if info.st_uid != os.getuid():
            raise SecureTreeStructuralError("snapshot-owner-invalid")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise SecureTreeStructuralError("snapshot-mode-invalid")
        if info.st_nlink != 1:
            raise SecureTreeStructuralError("snapshot-hard-link")
        if info.st_dev != root_device:
            raise SecureTreeStructuralError("snapshot-mount-crossing")
        return info

    @staticmethod
    def _entry_type(info: os.stat_result) -> str:
        if stat.S_ISDIR(info.st_mode):
            return "dir"
        if stat.S_ISREG(info.st_mode):
            return "file"
        if stat.S_ISLNK(info.st_mode):
            raise SecureTreeStructuralError("snapshot-symlink")
        raise SecureTreeStructuralError("snapshot-special-entry")

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() > deadline:
            raise SecureTreeRetryableError("snapshot-deadline")

    @staticmethod
    def _stat_name(parent: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            SecureTreeSnapshot._raise_retryable("snapshot-stat-failed", exc)

    @staticmethod
    def _fstat(descriptor: int) -> os.stat_result:
        try:
            return os.fstat(descriptor)
        except OSError as exc:
            SecureTreeSnapshot._raise_retryable("snapshot-fstat-failed", exc)

    def _relative_name(self, parent: str, name: str) -> str:
        self._validate_component(name, code="snapshot-name-invalid")
        relative = name if parent == "." else f"{parent}/{name}"
        try:
            encoded_relative = relative.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise SecureTreeStructuralError("snapshot-name-invalid") from exc
        if len(encoded_relative) > self._MAX_RELATIVE_PATH_BYTES:
            raise SecureTreeCapacityError(
                "snapshot-path-limit",
                metric="path_bytes",
                observed=len(encoded_relative),
                limit=self._MAX_RELATIVE_PATH_BYTES,
            )
        return relative

    def _read_exact_digest(self, descriptor: int, info: os.stat_result) -> str:
        remaining = info.st_size
        digest = hashlib.sha256()
        while remaining:
            self._check_deadline(self._deadline)
            try:
                chunk = os.read(descriptor, min(remaining, self._READ_CHUNK_BYTES))
            except OSError as exc:
                self._raise_retryable("snapshot-read-failed", exc)
            if not chunk:
                raise SecureTreeRetryableError("snapshot-file-truncated")
            digest.update(chunk)
            remaining -= len(chunk)
        try:
            extra = os.read(descriptor, 1)
        except OSError as exc:
            self._raise_retryable("snapshot-read-failed", exc)
        if extra:
            raise SecureTreeRetryableError("snapshot-file-grew")
        return digest.hexdigest()

    def _open_entry(self, parent: int, name: str, *, directory: bool) -> int:
        flags = _directory_open_flags() if directory else _file_open_flags()
        descriptor: int | None = None
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
            _verify_close_on_exec(descriptor)
            return descriptor
        except OSError as exc:
            if descriptor is not None:
                self._close_descriptor(descriptor)
            if exc.errno == errno.ELOOP:
                raise SecureTreeStructuralError("snapshot-symlink") from exc
            self._raise_retryable("snapshot-open-failed", exc)
        except SecureAccessError as exc:
            if descriptor is not None:
                self._close_descriptor(descriptor)
            raise SecureTreeStructuralError("snapshot-close-on-exec-failed") from exc

    def _count_file_bytes(self, info: os.stat_result) -> None:
        if stat.S_ISREG(info.st_mode):
            if info.st_size > self.limits.max_file_bytes:
                raise SecureTreeCapacityError(
                    "snapshot-file-limit",
                    metric="file_bytes",
                    observed=info.st_size,
                    limit=self.limits.max_file_bytes,
                )
            self._declared_bytes += info.st_size
            if self._declared_bytes > self.limits.max_total_bytes:
                raise SecureTreeCapacityError(
                    "snapshot-byte-limit",
                    metric="declared_bytes",
                    observed=self._declared_bytes,
                    limit=self.limits.max_total_bytes,
                )

    def _enumerate_names(self, descriptor: int) -> list[tuple[bytes, str]]:
        try:
            iterator = os.scandir(descriptor)
        except OSError as exc:
            self._raise_retryable("snapshot-scandir-failed", exc)
        retained: list[tuple[bytes, str]] = []
        try:
            try:
                for entry in iterator:
                    self._check_deadline(self._deadline)
                    encoded = self._validate_component(entry.name, code="snapshot-name-invalid")
                    self._descendants += 1
                    if self._descendants > self.limits.max_descendants:
                        raise SecureTreeCapacityError(
                            "snapshot-descendant-limit",
                            metric="descendants",
                            observed=self._descendants,
                            limit=self.limits.max_descendants,
                        )
                    retained.append((encoded, entry.name))
            except OSError as exc:
                self._raise_retryable("snapshot-scandir-failed", exc)
        finally:
            try:
                iterator.close()
            except OSError as exc:
                self._raise_retryable("snapshot-scandir-close-failed", exc)
        retained.sort(key=lambda item: item[0])
        return retained

    def _walk_directory(
        self,
        descriptor: int,
        relative: str,
        *,
        depth: int,
        root_device: int,
    ) -> None:
        self._check_deadline(self._deadline)
        before = self._checked_directory(
            self._fstat(descriptor),
            root_device=root_device,
            selected_root=relative == ".",
        )
        names = self._enumerate_names(descriptor)
        after_enumeration = self._checked_directory(
            self._fstat(descriptor),
            root_device=root_device,
            selected_root=relative == ".",
        )
        if _snapshot_identity(before) != _snapshot_identity(after_enumeration):
            raise SecureTreeRetryableError("snapshot-directory-mutated")
        for _encoded_name, name in names:
            child_relative = self._relative_name(relative, name)
            if self.detect_materialize_lock and name == "materialize.lock":
                raise SecureTreeStructuralError("materialize-lock-present")
            self._check_deadline(self._deadline)
            before_path = self._stat_name(descriptor, name)
            entry_type = self._entry_type(before_path)
            self._count_file_bytes(before_path)
            child_depth = depth + 1
            if child_depth > self.limits.max_depth:
                raise SecureTreeCapacityError(
                    "snapshot-depth-limit",
                    metric="depth",
                    observed=child_depth,
                    limit=self.limits.max_depth,
                )
            child_fd = self._open_entry(descriptor, name, directory=entry_type == "dir")
            content_digest: str | None = None
            try:
                opened = self._fstat(child_fd)
                if entry_type == "dir":
                    self._checked_directory(opened, root_device=root_device)
                else:
                    self._checked_regular(opened, root_device=root_device)
                if _snapshot_identity(before_path) != _snapshot_identity(opened):
                    raise SecureTreeRetryableError("snapshot-entry-replaced")
                if entry_type == "dir":
                    self._records.append(
                        {
                            "path": child_relative,
                            "type": "dir",
                            **_snapshot_metadata(opened),
                        }
                    )
                    self._semantic_records.append({"path": child_relative, "type": "dir"})
                    self._walk_directory(
                        child_fd,
                        child_relative,
                        depth=child_depth,
                        root_device=root_device,
                    )
                else:
                    if self.content_hash:
                        content_digest = self._read_exact_digest(child_fd, opened)
                    after_fd = self._fstat(child_fd)
                    if _snapshot_identity(opened) != _snapshot_identity(after_fd):
                        raise SecureTreeRetryableError("snapshot-file-mutated")
                    record: dict[str, str | int] = {
                        "path": child_relative,
                        "type": "file",
                        **_snapshot_metadata(opened),
                    }
                    if content_digest is not None:
                        record["sha256"] = content_digest
                    self._records.append(record)
                    semantic: dict[str, str] = {"path": child_relative, "type": "file"}
                    if content_digest is not None:
                        semantic["sha256"] = content_digest
                    self._semantic_records.append(semantic)
                after_fd = self._fstat(child_fd)
                after_path = self._stat_name(descriptor, name)
                if entry_type == "dir":
                    self._checked_directory(after_fd, root_device=root_device)
                    if self._entry_type(after_path) != "dir":
                        raise SecureTreeRetryableError("snapshot-entry-replaced")
                    self._checked_directory(after_path, root_device=root_device)
                else:
                    self._checked_regular(after_fd, root_device=root_device)
                    if self._entry_type(after_path) != "file":
                        raise SecureTreeRetryableError("snapshot-entry-replaced")
                    self._checked_regular(after_path, root_device=root_device)
                if _snapshot_identity(opened) != _snapshot_identity(after_fd) or _snapshot_identity(
                    opened
                ) != _snapshot_identity(after_path):
                    raise SecureTreeRetryableError("snapshot-entry-mutated")
            finally:
                self._close_descriptor(child_fd)
            after_child = self._checked_directory(
                self._fstat(descriptor),
                root_device=root_device,
                selected_root=relative == ".",
            )
            if _snapshot_identity(before) != _snapshot_identity(after_child):
                raise SecureTreeRetryableError("snapshot-directory-mutated")
        final = self._checked_directory(
            self._fstat(descriptor),
            root_device=root_device,
            selected_root=relative == ".",
        )
        if _snapshot_identity(before) != _snapshot_identity(final):
            raise SecureTreeRetryableError("snapshot-directory-mutated")

    def snapshot(self, *, deadline: float) -> SecureTreeResult:
        descriptor = self._require_open()
        self._deadline = deadline
        self._descendants = 0
        self._declared_bytes = 0
        root_info = self._checked_directory(
            self._fstat(descriptor),
            root_device=None,
            selected_root=True,
        )
        self._records: list[dict[str, str | int]] = [{"path": ".", "type": "dir", **_snapshot_metadata(root_info)}]
        self._semantic_records: list[dict[str, str]] = [{"path": ".", "type": "dir"}]
        self._walk_directory(descriptor, ".", depth=0, root_device=root_info.st_dev)
        self._records.sort(key=lambda item: (str(item["path"]).encode("utf-8"), str(item["type"])))
        self._semantic_records.sort(key=lambda item: (item["path"], item["type"]))
        return SecureTreeResult(
            stability_token=hashlib.sha256(_canonical_snapshot_bytes(self._records)).hexdigest(),
            semantic_digest=(
                hashlib.sha256(_canonical_snapshot_bytes(self._semantic_records)).hexdigest()
                if self.content_hash
                else None
            ),
            descendants=self._descendants,
            declared_bytes=self._declared_bytes,
        )

    def validate_canonical_root(self) -> None:
        current_identity = _snapshot_identity(self._fstat(self._require_open()))
        validation_fd = self._open_absolute_root()
        try:
            validation_identity = _snapshot_identity(self._fstat(validation_fd))
            if validation_identity != current_identity:
                raise SecureTreeStructuralError("snapshot-canonical-root-replaced")
        finally:
            self._close_descriptor(validation_fd)

    def require_same_directory_below(self, relative: str, expected: SecureTreeSnapshot) -> None:
        parts = _parts(relative)
        current: int | None = None
        try:
            current = os.dup(self._require_open())
            _verify_close_on_exec(current)
        except (OSError, SecureAccessError) as exc:
            if current is not None:
                self._close_descriptor(current)
            raise SecureTreeRetryableError("snapshot-qmd-root-open-failed") from exc
        try:
            for component in parts:
                child = self._open_entry(current, component, directory=True)
                previous = current
                current = child
                self._close_descriptor(previous)
            actual_info = self._fstat(current)
            expected_info = self._fstat(expected._require_open())
            if (actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino):
                raise SecureTreeStructuralError("snapshot-qmd-root-mismatch")
        finally:
            if current is not None:
                self._close_descriptor(current)


DATA_TREE_LIMITS = SecureTreeLimits(
    max_depth=8,
    max_descendants=4096,
    max_total_bytes=128 * 1024 * 1024,
    max_file_bytes=16 * 1024 * 1024,
)
CONFIG_TREE_LIMITS = SecureTreeLimits(
    max_depth=8,
    max_descendants=256,
    max_total_bytes=4 * 1024 * 1024,
    max_file_bytes=1024 * 1024,
)
QMD_TREE_LIMITS = SecureTreeLimits(
    max_depth=4,
    max_descendants=512,
    max_total_bytes=8 * 1024 * 1024,
    max_file_bytes=2 * 1024 * 1024,
)


def snapshot_secure_tree_group(
    *,
    data_root: Path,
    config_root: Path,
    qmd_root: Path,
    deadline_seconds: float = 5.0,
) -> SecureTreeGroupResult:
    """Return two equal complete observations under one descriptor authority."""
    if deadline_seconds <= 0:
        raise SecureTreeRetryableError("snapshot-deadline")
    deadline = time.monotonic() + deadline_seconds
    snapshots: list[SecureTreeSnapshot] = []
    try:
        snapshots.append(
            SecureTreeSnapshot(
                data_root,
                limits=DATA_TREE_LIMITS,
                content_hash=False,
                detect_materialize_lock=True,
            )
        )
        snapshots.append(
            SecureTreeSnapshot(
                config_root,
                limits=CONFIG_TREE_LIMITS,
                content_hash=False,
                detect_materialize_lock=True,
            )
        )
        snapshots.append(
            SecureTreeSnapshot(
                qmd_root,
                limits=QMD_TREE_LIMITS,
                content_hash=True,
                detect_materialize_lock=False,
            )
        )
        data, _config, qmd = snapshots
        data.require_same_directory_below(".qmd-docs", qmd)

        def validate_all() -> None:
            for selected in snapshots:
                selected.validate_canonical_root()
                SecureTreeSnapshot._check_deadline(deadline)

        first = tuple(selected.snapshot(deadline=deadline) for selected in snapshots)
        validate_all()
        second = tuple(selected.snapshot(deadline=deadline) for selected in snapshots)
        validate_all()
        if tuple(item.stability_token for item in first) != tuple(item.stability_token for item in second):
            raise SecureTreeRetryableError("snapshot-stability-mismatch")
        if first[2].semantic_digest != second[2].semantic_digest:
            raise SecureTreeRetryableError("snapshot-qmd-digest-mismatch")
        validate_all()
        return SecureTreeGroupResult(data=second[0], config=second[1], qmd=second[2])
    finally:
        first_close_error: BaseException | None = None
        for selected in reversed(snapshots):
            try:
                selected.close()
            except BaseException as exc:
                if first_close_error is None:
                    first_close_error = exc
        if first_close_error is not None:
            raise first_close_error


@dataclass(frozen=True)
class SecureFileInfo:
    relative_path: str
    size: int
    device: int
    inode: int


class SecureRoot:
    """An approved root traversed only with no-follow descriptor operations."""

    def __init__(self, path: Path, *, writable: bool, create: bool = False) -> None:
        self.path = Path(os.path.abspath(path))
        self.writable = writable
        flags = _directory_open_flags()
        current = os.open("/", flags)
        try:
            _verify_close_on_exec(current)
            parts = self.path.parts[1:]
            for index, component in enumerate(parts):
                final = index == len(parts) - 1
                if final and create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=current)
                try:
                    _verify_close_on_exec(child)
                except BaseException:
                    os.close(child)
                    raise
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
                    os.close(child)
                    raise SecureAccessError(f"unsafe approved-root component: {component}")
                os.close(current)
                current = child
        except OSError as exc:
            os.close(current)
            raise SecureAccessError(f"cannot open approved root {self.path}: {exc}") from exc
        except Exception:
            os.close(current)
            raise
        self._fd = current
        info = os.fstat(self._fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            os.close(self._fd)
            raise SecureAccessError(f"approved root is not a user-owned directory: {self.path}")
        if self.writable:
            os.fchmod(self._fd, 0o700)
            if stat.S_IMODE(os.fstat(self._fd).st_mode) != 0o700:
                os.close(self._fd)
                raise SecureAccessError(f"approved root is not mode 0700: {self.path}")

    def close(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            os.close(fd)
            self._fd = None

    def __enter__(self) -> SecureRoot:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open_dir(self, parts: Iterable[str], *, create: bool = False) -> int:
        current = os.dup(self._fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, _directory_open_flags(), dir_fd=current)
                except OSError as exc:
                    raise SecureAccessError(f"unsafe directory component {part!r} below {self.path}: {exc}") from exc
                os.close(current)
                current = child
                info = os.fstat(current)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    raise SecureAccessError(f"directory component is not user-owned: {part}")
                if self.writable:
                    os.fchmod(current, 0o700)
            return current
        except Exception:
            os.close(current)
            raise

    def _require_transaction(self, transaction: object) -> None:
        from observational_memory.search.generation_store import (
            _require_store_transaction,
        )

        record = _require_store_transaction(transaction, self.path)
        if self._fd is None:
            raise SecureAccessError("secure root is closed")
        anchor = os.fstat(self._fd)
        if (anchor.st_dev, anchor.st_ino) != (record.device, record.inode):
            raise SecureAccessError("store transaction does not lock this secure root")

    def ensure_directory(self, relative: str | Path, *, transaction: object) -> None:
        if not self.writable:
            raise SecureAccessError("read-only root cannot create directories")
        self._require_transaction(transaction)
        fd = self._open_dir(_parts(relative), create=True)
        os.close(fd)

    def list_entries(self) -> list[str]:
        """List names directly below this root without exposing its descriptor."""
        return sorted(os.listdir(self._fd))

    def inspect_safe_root_directory(self) -> None:
        """Require the opened input root to remain private from other writers."""
        if self._fd is None:
            raise SecureAccessError("secure root is closed")
        info = os.fstat(self._fd)
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise SecureAccessError(f"input directory is group/world writable: {self.path}")

    def acquire_store_transaction(self):
        """Lock an independent open-file description for this secure root."""
        if not self.writable:
            raise SecureAccessError("read-only root cannot own a store transaction")
        from observational_memory.search.generation_store import (
            _acquire_store_transaction_from_fd,
        )

        return _acquire_store_transaction_from_fd(
            self.path,
            self._fd,
            timeout_seconds=0.0,
        )

    def list_directory(self, relative: str | Path) -> list[str]:
        fd = self._open_dir(_parts(relative))
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)

    def inspect_safe_directory(self, relative: str | Path) -> None:
        """Require a no-follow, user-owned directory that others cannot modify."""
        fd = self._open_dir(_parts(relative))
        try:
            info = os.fstat(fd)
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise SecureAccessError(f"input directory is group/world writable: {relative}")
        finally:
            os.close(fd)

    def inspect_regular_file(self, relative: str | Path) -> SecureFileInfo:
        """Validate an input leaf from metadata only, without reading its content."""
        parent, name = self._parent_and_name(relative)
        try:
            info = self._stat_leaf(parent, name)
            if info is None:
                raise FileNotFoundError(str(relative))
            self._require_regular_owned_single_link(info, relative)
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise SecureAccessError(f"input file is group/world writable: {relative}")
            return SecureFileInfo(
                relative_path=str(PurePosixPath(*_parts(relative))),
                size=info.st_size,
                device=info.st_dev,
                inode=info.st_ino,
            )
        finally:
            os.close(parent)

    def has_secure_regular_file(self, relative: str | Path) -> bool:
        """Return whether a leaf is a securely eligible regular input file."""
        try:
            self.inspect_regular_file(relative)
        except (FileNotFoundError, SecureAccessError):
            return False
        return True

    def _parent_and_name(self, relative: str | Path, *, create_parent: bool = False) -> tuple[int, str]:
        parts = _parts(relative)
        parent = self._open_dir(parts[:-1], create=create_parent) if len(parts) > 1 else os.dup(self._fd)
        return parent, parts[-1]

    def _stat_leaf(self, parent: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _require_regular_owned_single_link(info: os.stat_result, relative: str | Path) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise SecureAccessError(f"file is not regular: {relative}")
        if info.st_uid != os.getuid():
            raise SecureAccessError(f"file has an unexpected owner: {relative}")
        if info.st_nlink != 1:
            raise SecureAccessError(f"file has an unsafe link count: {relative}")

    def read_bytes(self, relative: str | Path, *, max_bytes: int | None = None) -> tuple[bytes, SecureFileInfo]:
        """Read the exact initial size and prove leaf identity stayed stable."""
        parent, name = self._parent_and_name(relative)
        fd: int | None = None
        try:
            before_path = self._stat_leaf(parent, name)
            if before_path is None:
                raise FileNotFoundError(str(relative))
            self._require_regular_owned_single_link(before_path, relative)
            if not self.writable and stat.S_IMODE(before_path.st_mode) & 0o022:
                raise SecureAccessError(f"input file is group/world writable: {relative}")
            if max_bytes is not None and before_path.st_size > max_bytes:
                raise SecureAccessError(f"file exceeds {max_bytes} byte limit: {relative}")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError as exc:
                raise SecureAccessError(f"cannot securely open {relative}: {exc}") from exc
            before_fd = os.fstat(fd)
            self._require_regular_owned_single_link(before_fd, relative)
            if _identity(before_path) != _identity(before_fd):
                raise UnstableReadError(f"file was replaced before read: {relative}")

            remaining = before_fd.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    raise UnstableReadError(f"file was truncated during read: {relative}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise UnstableReadError(f"file grew during read: {relative}")

            after_fd = os.fstat(fd)
            after_path = self._stat_leaf(parent, name)
            if after_path is None:
                raise UnstableReadError(f"file was removed during read: {relative}")
            if _identity(before_fd) != _identity(after_fd) or _identity(before_fd) != _identity(after_path):
                raise UnstableReadError(f"file metadata or identity changed during read: {relative}")
            return b"".join(chunks), SecureFileInfo(
                relative_path=str(PurePosixPath(*_parts(relative))),
                size=before_fd.st_size,
                device=before_fd.st_dev,
                inode=before_fd.st_ino,
            )
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def _quarantine_leaf(self, parent: int, name: str, relative: str | Path) -> None:
        if not self.writable:
            raise SecureAccessError(f"unsafe read-only leaf: {relative}")
        quarantine = self._open_dir(("quarantine",), create=True)
        quarantine_name = f"{name}.{uuid.uuid4().hex}.unsafe"
        try:
            os.rename(name, quarantine_name, src_dir_fd=parent, dst_dir_fd=quarantine)
        except OSError as exc:
            raise SecureAccessError(f"cannot quarantine unsafe leaf {relative}: {exc}") from exc
        finally:
            os.close(quarantine)

    def secure_output_leaf(self, relative: str | Path, *, transaction: object) -> bool:
        """Harden a safe legacy leaf or quarantine an unsafe leaf without reading."""
        if not self.writable:
            raise SecureAccessError("read-only root has no output leaves")
        self._require_transaction(transaction)
        parent, name = self._parent_and_name(relative, create_parent=True)
        try:
            info = self._stat_leaf(parent, name)
            if info is None:
                return False
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                self._quarantine_leaf(parent, name, relative)
                return False
            os.chmod(name, 0o600, dir_fd=parent, follow_symlinks=False)
            hardened = self._stat_leaf(parent, name)
            if hardened is None:
                raise SecureAccessError(f"output leaf disappeared while securing: {relative}")
            self._require_regular_owned_single_link(hardened, relative)
            if stat.S_IMODE(hardened.st_mode) != 0o600:
                raise SecureAccessError(f"cannot enforce mode 0600 on output leaf: {relative}")
            return True
        finally:
            os.close(parent)

    def establish_boundary(self, leaves: Iterable[str | Path], *, transaction: object) -> None:
        """Establish 0700 directories and 0600 safe leaves before state use."""
        if not self.writable:
            raise SecureAccessError("cannot establish output boundary on read-only root")
        self._require_transaction(transaction)
        root_info = os.fstat(self._fd)
        os.fchmod(self._fd, 0o700)
        if root_info.st_uid != os.getuid():
            raise SecureAccessError("bridge state directory has an unexpected owner")
        for relative in leaves:
            self.secure_output_leaf(relative, transaction=transaction)

    def read_output_bytes(
        self,
        relative: str | Path,
        *,
        transaction: object,
        max_bytes: int | None = None,
    ) -> bytes | None:
        if not self.secure_output_leaf(relative, transaction=transaction):
            return None
        data, _info = self.read_bytes(relative, max_bytes=max_bytes)
        return data

    def atomic_write_bytes(self, relative: str | Path, data: bytes, *, transaction: object) -> None:
        if not self.writable:
            raise SecureAccessError("read-only root cannot write")
        self._require_transaction(transaction)
        self.secure_output_leaf(relative, transaction=transaction)
        parent, name = self._parent_and_name(relative, create_parent=True)
        temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        fd: int | None = None
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            _write_all(fd, data)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            self._require_regular_owned_single_link(info, relative)
            os.close(fd)
            fd = None
            os.rename(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            if not self.secure_output_leaf(relative, transaction=transaction):
                raise SecureAccessError(f"atomic output disappeared: {relative}")
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temp_name, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def write_new_bytes(self, relative: str | Path, data: bytes, *, transaction: object) -> None:
        """Create one immutable output leaf and fail if that name already exists."""
        if not self.writable:
            raise SecureAccessError("read-only root cannot write")
        self._require_transaction(transaction)
        parent, name = self._parent_and_name(relative, create_parent=True)
        fd: int | None = None
        created = False
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            created = True
            _write_all(fd, data)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            self._require_regular_owned_single_link(os.fstat(fd), relative)
            os.close(fd)
            fd = None
            os.fsync(parent)
        except Exception:
            if fd is not None:
                os.close(fd)
            if created:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(parent)

    def append_bytes(self, relative: str | Path, data: bytes, *, transaction: object) -> None:
        if not self.writable:
            raise SecureAccessError("read-only root cannot append")
        self._require_transaction(transaction)
        self.secure_output_leaf(relative, transaction=transaction)
        parent, name = self._parent_and_name(relative, create_parent=True)
        fd: int | None = None
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            info = os.fstat(fd)
            self._require_regular_owned_single_link(info, relative)
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            after = os.fstat(fd)
            self._require_regular_owned_single_link(after, relative)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def quarantine_output_leaf(self, relative: str | Path, *, transaction: object) -> None:
        """Move an output leaf aside without opening or reading it."""
        self._require_transaction(transaction)
        parent, name = self._parent_and_name(relative, create_parent=True)
        try:
            if self._stat_leaf(parent, name) is not None:
                self._quarantine_leaf(parent, name, relative)
        finally:
            os.close(parent)


def assert_disjoint_output(output_root: Path, native_roots: Iterable[Path]) -> None:
    """Prove the approved output root and every protected root are disjoint."""
    output_real = Path(os.path.realpath(output_root))
    for native_root in native_roots:
        native_real = Path(os.path.realpath(native_root))
        try:
            common = Path(os.path.commonpath([output_real, native_real]))
        except ValueError:
            continue
        if common in {native_real, output_real}:
            raise SecureAccessError(f"bridge output must be disjoint from protected root {native_root}")
