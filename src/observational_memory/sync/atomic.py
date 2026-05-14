"""Atomic filesystem helpers for cluster state."""

from __future__ import annotations

import errno
import os
import time
import uuid
from pathlib import Path


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write bytes via same-directory temp file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(temp), flags, mode if mode is not None else 0o666)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        if mode is not None:
            temp.chmod(mode)
        os.replace(temp, path)
        _fsync_parent(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


class DirectoryLock:
    """Portable coarse lock implemented as atomic directory creation."""

    def __init__(self, path: Path, *, timeout_seconds: float = 10.0, stale_seconds: float = 3600.0):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self._held = False

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
                atomic_write_text(self.path / "owner", f"pid={os.getpid()}\ncreated={time.time()}\n")
                self._held = True
                return
            except FileExistsError:
                self._cleanup_stale_lock()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if not self._held:
            return
        try:
            for child in self.path.iterdir():
                child.unlink()
            self.path.rmdir()
        except FileNotFoundError:
            pass
        finally:
            self._held = False

    def _cleanup_stale_lock(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
            return
        if time.time() - stat.st_mtime < self.stale_seconds:
            return
        try:
            for child in self.path.iterdir():
                child.unlink()
            self.path.rmdir()
        except OSError:
            return

    def __enter__(self) -> DirectoryLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
