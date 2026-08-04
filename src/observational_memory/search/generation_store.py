"""Secure root-descriptor transactions and generation-store file primitives."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GenerationStoreError(RuntimeError):
    """The generation store is missing, unsafe, or incoherent."""


class StoreBusyError(GenerationStoreError):
    """Another cooperating writer owns the store-root descriptor lock."""


class StoreLockUnsupportedError(GenerationStoreError):
    """The host cannot enforce the approved store-root descriptor lock."""


class _PointerChanged(RuntimeError):
    """A safe pointer leaf was atomically replaced during one read attempt."""


@dataclass
class _TransactionRecord:
    root: Path
    fd: int
    device: int
    inode: int
    owner: int
    mode: int
    pid: int
    thread_id: int
    thread_owner: threading.Thread
    state: str


@dataclass
class _ReadRootRecord:
    root: Path
    fd: int
    device: int
    inode: int
    pid: int
    active: bool


_TRANSACTIONS: weakref.WeakKeyDictionary[object, _TransactionRecord] = weakref.WeakKeyDictionary()
_READ_ROOTS: weakref.WeakKeyDictionary[object, _ReadRootRecord] = weakref.WeakKeyDictionary()
_REGISTRY_LOCK = threading.Lock()

_TRANSACTION_ACTIVE = "active"
_TRANSACTION_RELEASING = "releasing"
_TRANSACTION_RELEASED = "released"


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise StoreLockUnsupportedError("generation store requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC")
    if not hasattr(fcntl, "F_GETFD") or not hasattr(fcntl, "FD_CLOEXEC"):
        raise StoreLockUnsupportedError("generation store cannot verify close-on-exec")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _verify_close_on_exec(fd: int) -> None:
    try:
        descriptor_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError as exc:
        raise StoreLockUnsupportedError("generation store cannot verify close-on-exec") from exc
    if descriptor_flags & fcntl.FD_CLOEXEC == 0:
        raise StoreLockUnsupportedError("generation store descriptor is not close-on-exec")


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


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _require_directory(
    info: os.stat_result,
    label: str,
    *,
    require_user: bool = True,
    require_mode: bool = True,
) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise GenerationStoreError(f"generation store component is not a directory: {label}")
    if require_user and info.st_uid != os.getuid():
        raise GenerationStoreError(f"generation store component has an unexpected owner: {label}")
    if require_mode and stat.S_IMODE(info.st_mode) != 0o700:
        raise GenerationStoreError(f"generation store directory is not mode 0700: {label}")


def _require_regular(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise GenerationStoreError(f"generation store leaf is not regular: {label}")
    if info.st_uid != os.getuid():
        raise GenerationStoreError(f"generation store leaf has an unexpected owner: {label}")
    if info.st_nlink != 1:
        raise GenerationStoreError(f"generation store leaf has an unsafe link count: {label}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise GenerationStoreError(f"generation store leaf is not mode 0600: {label}")


def _secure_open_root(
    root: Path,
    *,
    create: bool,
    writable: bool,
    require_mode: bool = True,
) -> int:
    """Open an absolute root with no-follow traversal and no path reopening later."""
    selected = Path(os.path.abspath(root))
    flags = _directory_open_flags()
    current = os.open("/", flags)
    try:
        _verify_close_on_exec(current)
        parts = selected.parts[1:]
        if not parts:
            raise GenerationStoreError("generation store root cannot be the filesystem root")
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
            if final:
                _require_directory(info, str(selected), require_mode=False)
                if writable:
                    os.fchmod(child, 0o700)
                _require_directory(
                    os.fstat(child),
                    str(selected),
                    require_mode=require_mode,
                )
            elif not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
                os.close(child)
                raise GenerationStoreError(f"unsafe generation-store parent component: {component}")
            os.close(current)
            current = child
        return current
    except FileNotFoundError:
        os.close(current)
        raise
    except OSError as exc:
        os.close(current)
        raise GenerationStoreError(f"cannot open generation store {selected}: {exc}") from exc
    except Exception:
        os.close(current)
        raise


def _validate_root_fd(root: Path, fd: int, *, require_mode: bool = True) -> os.stat_result:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise GenerationStoreError(f"generation store descriptor is invalid: {exc}") from exc
    _require_directory(info, str(root), require_mode=require_mode)
    return info


def _acquire_flock(fd: int, *, timeout_seconds: float) -> None:
    if not hasattr(fcntl, "flock") or not hasattr(fcntl, "LOCK_EX") or not hasattr(fcntl, "LOCK_NB"):
        raise StoreLockUnsupportedError("store-root descriptor locking is unsupported")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            unsupported = {
                getattr(errno, "ENOTSUP", -1),
                getattr(errno, "EOPNOTSUPP", -1),
                getattr(errno, "ENOSYS", -1),
                getattr(errno, "EINVAL", -1),
            }
            if exc.errno in unsupported:
                raise StoreLockUnsupportedError("store-root descriptor locking is unsupported") from exc
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise GenerationStoreError(f"store-root descriptor lock failed: {exc}") from exc
            if timeout_seconds <= 0 or time.monotonic() >= deadline:
                raise StoreBusyError("store-root descriptor lock is busy") from exc
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        raise GenerationStoreError(f"store-root descriptor unlock failed: {exc}") from exc


def _try_generation_lock(fd: int, *, exclusive: bool) -> bool:
    """Pin one generation for reading or claim it for nonblocking pruning."""
    if not hasattr(fcntl, "LOCK_SH"):
        raise StoreLockUnsupportedError("generation reader locking is unsupported")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except OSError as exc:
        unsupported = {
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
            getattr(errno, "ENOSYS", -1),
            getattr(errno, "EINVAL", -1),
        }
        if exc.errno in unsupported:
            raise StoreLockUnsupportedError("generation reader locking is unsupported") from exc
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise GenerationStoreError(f"generation descriptor lock failed: {exc}") from exc
    return True


def _current_thread_owner() -> tuple[int, threading.Thread]:
    """Return the required numeric ID and the non-recyclable thread owner."""
    return threading.get_ident(), threading.current_thread()


class StoreTransaction:
    """Opaque process- and thread-bound owner of one locked store root."""

    __slots__ = ("__weakref__",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("store transactions are minted only by secure root acquisition")

    @classmethod
    def acquire(
        cls,
        root: Path,
        *,
        timeout_seconds: float = 0.0,
        create: bool = True,
    ) -> StoreTransaction:
        fd = _secure_open_root(root, create=create, writable=True)
        return _mint_transaction(Path(os.path.abspath(root)), fd, timeout_seconds=timeout_seconds)

    def release(self) -> None:
        _release_store_transaction(self)

    def __enter__(self) -> StoreTransaction:
        _require_store_transaction(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __copy__(self):
        raise TypeError("store transactions cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("store transactions cannot be copied")

    def __reduce__(self):
        raise TypeError("store transactions cannot be pickled")

    @property
    def root(self) -> Path:
        return _require_store_transaction(self).root

    @property
    def device(self) -> int:
        return _require_store_transaction(self).device

    @property
    def inode(self) -> int:
        return _require_store_transaction(self).inode

    @property
    def active(self) -> bool:
        with _REGISTRY_LOCK:
            record = _TRANSACTIONS.get(self)
            return bool(record is not None and record.state == _TRANSACTION_ACTIVE)


class StoreReadRoot:
    """Opaque descriptor used by the fixed reader without publication authority."""

    __slots__ = ("__weakref__",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("store read roots are minted only by secure root acquisition")

    @classmethod
    def open(cls, root: Path) -> StoreReadRoot:
        selected = Path(os.path.abspath(root))
        fd = _secure_open_root(selected, create=False, writable=False)
        info = _validate_root_fd(selected, fd)
        handle = object.__new__(cls)
        with _REGISTRY_LOCK:
            _READ_ROOTS[handle] = _ReadRootRecord(
                root=selected,
                fd=fd,
                device=info.st_dev,
                inode=info.st_ino,
                pid=os.getpid(),
                active=True,
            )
        return handle

    def close(self) -> None:
        with _REGISTRY_LOCK:
            record = _READ_ROOTS.get(self)
            if record is None or not record.active:
                return
            record.active = False
            fd = record.fd
            record.fd = -1
        os.close(fd)

    def __enter__(self) -> StoreReadRoot:
        _require_read_root(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __copy__(self):
        raise TypeError("store read roots cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("store read roots cannot be copied")

    def __reduce__(self):
        raise TypeError("store read roots cannot be pickled")

    @property
    def root(self) -> Path:
        return _require_read_root(self).root


def _mint_transaction(root: Path, fd: int, *, timeout_seconds: float) -> StoreTransaction:
    """Mint a transaction around an owned descriptor and close it on every failure."""
    locked = False
    transaction: StoreTransaction | None = None
    record: _TransactionRecord | None = None
    try:
        info = _validate_root_fd(root, fd)
        _verify_close_on_exec(fd)
        identity_key = (info.st_dev, info.st_ino)
        _acquire_flock(fd, timeout_seconds=timeout_seconds)
        locked = True
        info = _validate_root_fd(root, fd)
        if (info.st_dev, info.st_ino) != identity_key:
            raise GenerationStoreError("store root descriptor identity changed during lock acquisition")
        thread_id, thread_owner = _current_thread_owner()
        transaction = object.__new__(StoreTransaction)
        record = _TransactionRecord(
            root=root,
            fd=fd,
            device=info.st_dev,
            inode=info.st_ino,
            owner=info.st_uid,
            mode=stat.S_IMODE(info.st_mode),
            pid=os.getpid(),
            thread_id=thread_id,
            thread_owner=thread_owner,
            state=_TRANSACTION_ACTIVE,
        )
        with _REGISTRY_LOCK:
            _TRANSACTIONS[transaction] = record
        validate_canonical_root(transaction)
        return transaction
    except BaseException as original:
        cleanup_failures: list[str] = []
        if transaction is not None and record is not None:
            with _REGISTRY_LOCK:
                registered = _TRANSACTIONS.get(transaction)
                if registered is record:
                    record.state = _TRANSACTION_RELEASING
        if locked:
            try:
                _unlock(fd)
            except BaseException as cleanup_error:
                cleanup_failures.append(f"unlock: {cleanup_error}")
        try:
            os.close(fd)
        except BaseException as cleanup_error:
            cleanup_failures.append(f"close: {cleanup_error}")
        finally:
            if transaction is not None and record is not None:
                with _REGISTRY_LOCK:
                    registered = _TRANSACTIONS.get(transaction)
                    if registered is record:
                        record.fd = -1
                        record.state = _TRANSACTION_RELEASED
        if cleanup_failures:
            raise GenerationStoreError(
                f"{type(original).__name__}: {original}; transaction cleanup failed: " + "; ".join(cleanup_failures)
            ) from original
        raise


def _acquire_store_transaction_from_fd(
    root: Path,
    secure_root_fd: int,
    *,
    timeout_seconds: float = 0.0,
) -> StoreTransaction:
    """Bind a transaction to an already-open secure root without exposing its fd."""
    selected = Path(os.path.abspath(root))
    anchor = _validate_root_fd(selected, secure_root_fd)
    flags = _directory_open_flags()
    try:
        independent = os.open(".", flags, dir_fd=secure_root_fd)
    except OSError as exc:
        raise GenerationStoreError(f"cannot independently open secure store root {selected}: {exc}") from exc
    try:
        _verify_close_on_exec(independent)
        opened = _validate_root_fd(selected, independent)
        if (opened.st_dev, opened.st_ino) != (anchor.st_dev, anchor.st_ino):
            raise GenerationStoreError("independent store-root descriptor does not match the secure anchor")
    except BaseException:
        os.close(independent)
        raise
    return _mint_transaction(selected, independent, timeout_seconds=timeout_seconds)


def _require_store_transaction(
    transaction: object,
    expected_root: Path | None = None,
) -> _TransactionRecord:
    with _REGISTRY_LOCK:
        record = _TRANSACTIONS.get(transaction)
    if record is None:
        raise RuntimeError("BM25 publication requires an active StoreTransaction")
    if record.state == _TRANSACTION_RELEASED or record.fd < 0:
        raise RuntimeError("StoreTransaction has been released")
    if record.state != _TRANSACTION_ACTIVE:
        raise RuntimeError("StoreTransaction is releasing")
    if record.pid != os.getpid():
        raise RuntimeError("StoreTransaction belongs to a different process")
    thread_id, thread_owner = _current_thread_owner()
    if record.thread_id != thread_id or record.thread_owner is not thread_owner:
        raise RuntimeError("StoreTransaction belongs to a different thread")
    if expected_root is not None and record.root != Path(os.path.abspath(expected_root)):
        raise RuntimeError("StoreTransaction belongs to a different store root")
    info = _validate_root_fd(record.root, record.fd)
    if (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        stat.S_IMODE(info.st_mode),
    ) != (
        record.device,
        record.inode,
        record.owner,
        record.mode,
    ):
        raise RuntimeError("StoreTransaction root descriptor identity changed")
    return record


def _require_read_root(read_root: object, expected_root: Path | None = None) -> _ReadRootRecord:
    with _REGISTRY_LOCK:
        record = _READ_ROOTS.get(read_root)
    if record is None or not record.active or record.fd < 0:
        raise RuntimeError("BM25 read requires an active StoreReadRoot")
    if record.pid != os.getpid():
        raise RuntimeError("StoreReadRoot belongs to a different process")
    if expected_root is not None and record.root != Path(os.path.abspath(expected_root)):
        raise RuntimeError("StoreReadRoot belongs to a different store root")
    info = _validate_root_fd(record.root, record.fd)
    if (info.st_dev, info.st_ino) != (record.device, record.inode):
        raise RuntimeError("StoreReadRoot descriptor identity changed")
    return record


def _release_store_transaction(transaction: object) -> None:
    with _REGISTRY_LOCK:
        record = _TRANSACTIONS.get(transaction)
        if record is None:
            raise RuntimeError("unknown StoreTransaction")
        if record.state == _TRANSACTION_RELEASED:
            return
        if record.state != _TRANSACTION_ACTIVE:
            raise RuntimeError("StoreTransaction is already releasing")
        thread_id, thread_owner = _current_thread_owner()
        if record.pid != os.getpid() or record.thread_id != thread_id or record.thread_owner is not thread_owner:
            raise RuntimeError("StoreTransaction can be released only by its owning process and thread")
        record.state = _TRANSACTION_RELEASING
        fd = record.fd
    cleanup_failures: list[str] = []
    try:
        try:
            _unlock(fd)
        except BaseException as cleanup_error:
            cleanup_failures.append(f"unlock: {cleanup_error}")
        try:
            os.close(fd)
        except BaseException as cleanup_error:
            cleanup_failures.append(f"close: {cleanup_error}")
    finally:
        with _REGISTRY_LOCK:
            record.fd = -1
            record.state = _TRANSACTION_RELEASED
    if cleanup_failures:
        raise GenerationStoreError("store-root transaction release failed: " + "; ".join(cleanup_failures))


def validate_canonical_root(transaction: StoreTransaction) -> None:
    """Prove the canonical path still names the locked descriptor identity."""
    record = _require_store_transaction(transaction)
    _validate_canonical_identity(record.root, record.device, record.inode)


def _validate_canonical_read_root(read_root: StoreReadRoot) -> None:
    """Prove a read-only anchor still names the canonical store root."""
    record = _require_read_root(read_root)
    _validate_canonical_identity(record.root, record.device, record.inode)


def _validate_canonical_identity(root: Path, device: int, inode: int) -> None:
    reopened = _secure_open_root(root, create=False, writable=False)
    try:
        info = _validate_root_fd(root, reopened)
        if (info.st_dev, info.st_ino) != (device, inode):
            raise GenerationStoreError("canonical store root no longer names the locked descriptor")
    finally:
        os.close(reopened)


def _duplicate_root_fd(
    handle: StoreTransaction | StoreReadRoot,
    *,
    expected_root: Path,
) -> int:
    if isinstance(handle, StoreTransaction):
        record = _require_store_transaction(handle, expected_root)
    elif isinstance(handle, StoreReadRoot):
        record = _require_read_root(handle, expected_root)
    else:
        raise RuntimeError("BM25 store requires a StoreTransaction or StoreReadRoot")
    return os.dup(record.fd)


def _open_dir(parent: int, name: str, *, create: bool, writable: bool) -> int:
    if "/" in name or name in {"", ".", ".."}:
        raise GenerationStoreError(f"unsafe generation directory name: {name!r}")
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _directory_open_flags(), dir_fd=parent)
    except OSError as exc:
        raise GenerationStoreError(f"cannot open generation directory {name!r}: {exc}") from exc
    try:
        if writable:
            os.fchmod(fd, 0o700)
        _require_directory(os.fstat(fd), name)
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_file(
    parent: int,
    name: str,
    *,
    max_bytes: int = 128 * 1024 * 1024,
    retry_atomic_replacement: bool = False,
) -> bytes:
    try:
        before_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise GenerationStoreError(f"cannot inspect generation leaf {name!r}: {exc}") from exc
    _require_regular(before_path, name)
    if before_path.st_size > max_bytes:
        raise GenerationStoreError(f"generation leaf exceeds {max_bytes} bytes: {name}")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise GenerationStoreError(f"cannot open generation leaf {name!r}: {exc}") from exc
    try:
        before_fd = os.fstat(fd)
        _require_regular(before_fd, name)
        if _identity(before_path) != _identity(before_fd):
            if retry_atomic_replacement and (before_path.st_dev, before_path.st_ino) != (
                before_fd.st_dev,
                before_fd.st_ino,
            ):
                raise _PointerChanged(name)
            raise GenerationStoreError(f"generation leaf changed before read: {name}")
        remaining = before_fd.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise GenerationStoreError(f"generation leaf was truncated: {name}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise GenerationStoreError(f"generation leaf grew while read: {name}")
        after_fd = os.fstat(fd)
        if _identity(before_fd) != _identity(after_fd):
            raise GenerationStoreError(f"generation leaf changed while read: {name}")
        try:
            after_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise GenerationStoreError(f"cannot inspect generation leaf after read {name!r}: {exc}") from exc
        _require_regular(after_path, name)
        if _identity(before_fd) != _identity(after_path):
            if retry_atomic_replacement and (before_fd.st_dev, before_fd.st_ino) != (
                after_path.st_dev,
                after_path.st_ino,
            ):
                raise _PointerChanged(name)
            raise GenerationStoreError(f"generation leaf changed while read: {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_file(parent: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        _write_all(fd, data)
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        _require_regular(os.fstat(fd), name)
    finally:
        os.close(fd)
