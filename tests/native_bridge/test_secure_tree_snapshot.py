from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import observational_memory.native_bridge.secure_fs as secure_fs
from observational_memory.native_bridge.secure_fs import (
    SecureTreeCapacityError,
    SecureTreeLimits,
    SecureTreeRetryableError,
    SecureTreeSnapshot,
    SecureTreeStructuralError,
    snapshot_secure_tree_group,
)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "data"
    config = tmp_path / "config"
    qmd = data / ".qmd-docs"
    qmd.mkdir(parents=True)
    config.mkdir()
    return data, config, qmd


def _single_snapshot(
    root: Path,
    *,
    content_hash: bool = False,
    limits: SecureTreeLimits | None = None,
) -> SecureTreeSnapshot:
    return SecureTreeSnapshot(
        root,
        limits=limits or SecureTreeLimits(8, 64, 1024 * 1024, 1024 * 1024),
        content_hash=content_hash,
        detect_materialize_lock=False,
    )


# Invariant: an iterator error after a yielded name propagates and produces no snapshot.
def test_scandir_error_after_partial_yield_is_retryable(tmp_path, monkeypatch):
    data, config, qmd = _roots(tmp_path)
    (data / "first").write_text("one")
    (data / "second").write_text("two")
    original_scandir = secure_fs.os.scandir
    attacked = False

    class PartialFailure:
        def __init__(self, entries):
            self.entries = iter(entries)
            self.returned = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self.returned:
                self.returned = True
                return next(self.entries)
            raise OSError("injected enumeration failure")

        def close(self):
            return None

    def failing_scandir(descriptor):
        nonlocal attacked
        iterator = original_scandir(descriptor)
        entries = list(iterator)
        iterator.close()
        if not attacked:
            attacked = True
            return PartialFailure(entries)
        return original_scandir(descriptor)

    monkeypatch.setattr(secure_fs.os, "scandir", failing_scandir)

    with pytest.raises(SecureTreeRetryableError, match="snapshot-scandir-failed"):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: path replacement between stat and open is never accepted.
@pytest.mark.parametrize("entry_type", ["file", "directory"])
def test_file_or_directory_replacement_is_retryable(tmp_path, monkeypatch, entry_type):
    data, _config, _qmd = _roots(tmp_path)
    victim = data / "victim"
    if entry_type == "file":
        victim.write_text("old")
    else:
        victim.mkdir()
    original_open_entry = SecureTreeSnapshot._open_entry
    attacked = False

    def replacing_open(self, parent, name, *, directory):
        nonlocal attacked
        if self.path == data and name == "victim" and not attacked:
            attacked = True
            victim.rename(data / "old-victim")
            victim.mkdir() if directory else victim.write_text("new")
        return original_open_entry(self, parent, name, directory=directory)

    monkeypatch.setattr(SecureTreeSnapshot, "_open_entry", replacing_open)

    with pytest.raises(SecureTreeRetryableError, match="snapshot-entry-replaced"):
        with _single_snapshot(data) as snapshot:
            snapshot.snapshot(deadline=time.monotonic() + 5)


# Invariant: links and special entries cannot enter a successful snapshot.
@pytest.mark.parametrize("attack", ["symlink", "fifo", "hardlink"])
def test_symlink_special_and_hardlink_are_structural(tmp_path, attack):
    data, config, qmd = _roots(tmp_path)
    selected = data / "unsafe"
    if attack == "symlink":
        selected.symlink_to(config, target_is_directory=True)
        expected = "snapshot-symlink"
    elif attack == "fifo":
        os.mkfifo(selected)
        expected = "snapshot-special-entry"
    else:
        outside = tmp_path / "outside"
        outside.write_text("linked")
        os.link(outside, selected)
        expected = "snapshot-hard-link"

    with pytest.raises(SecureTreeStructuralError, match=expected):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: ownership and mount checks compare every descendant with the selected root.
def test_wrong_owner_and_mount_crossing_are_structural():
    regular = stat.S_IFREG | 0o644
    wrong_owner = SimpleNamespace(
        st_mode=regular,
        st_uid=os.getuid() + 1,
        st_nlink=1,
        st_dev=1,
    )
    crossing = SimpleNamespace(
        st_mode=regular,
        st_uid=os.getuid(),
        st_nlink=1,
        st_dev=2,
    )

    with pytest.raises(SecureTreeStructuralError, match="snapshot-owner-invalid"):
        SecureTreeSnapshot._checked_regular(wrong_owner, root_device=1)
    with pytest.raises(SecureTreeStructuralError, match="snapshot-mount-crossing"):
        SecureTreeSnapshot._checked_regular(crossing, root_device=1)


# Invariant: group- or world-writable descendants are structural stops.
def test_group_writable_descendant_is_structural(tmp_path):
    data, config, qmd = _roots(tmp_path)
    selected = data / "unsafe"
    selected.write_text("mutable")
    selected.chmod(0o664)

    with pytest.raises(SecureTreeStructuralError, match="snapshot-mode-invalid"):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: a lock component is a structural stop regardless of its type.
@pytest.mark.parametrize("lock_type", ["file", "directory"])
def test_materialize_lock_file_or_directory_is_structural(tmp_path, lock_type):
    data, config, qmd = _roots(tmp_path)
    lock = config / "materialize.lock"
    lock.write_text("locked") if lock_type == "file" else lock.mkdir()

    with pytest.raises(SecureTreeStructuralError, match="materialize-lock-present"):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: a negative lock result requires two complete passes of all three roots.
def test_negative_group_runs_two_complete_passes_per_root(tmp_path, monkeypatch):
    data, config, qmd = _roots(tmp_path)
    (data / "memory.md").write_text("memory")
    (config / "env").write_text("config")
    (qmd / "profile.md").write_text("qmd")
    original_snapshot = SecureTreeSnapshot.snapshot
    calls: dict[Path, int] = {}

    def counted_snapshot(self, *, deadline):
        calls[self.path] = calls.get(self.path, 0) + 1
        return original_snapshot(self, deadline=deadline)

    monkeypatch.setattr(SecureTreeSnapshot, "snapshot", counted_snapshot)

    result = snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)

    assert result.qmd.semantic_digest is not None
    assert calls == {data: 2, config: 2, qmd: 2}


# Invariant: truncation, growth, replacement, and read errors fail the QMD snapshot.
@pytest.mark.parametrize("attack", ["truncate", "grow", "replace", "read-error"])
def test_qmd_read_failures_are_retryable(tmp_path, monkeypatch, attack):
    data, config, qmd = _roots(tmp_path)
    selected = qmd / "profile.md"
    selected.write_bytes(b"x" * 1024)
    original_read = secure_fs.os.read
    original_inode = selected.stat().st_ino
    attacked = False

    def attacking_read(descriptor, size):
        nonlocal attacked
        info = os.fstat(descriptor)
        if info.st_ino == original_inode and attack == "read-error":
            raise OSError("injected read failure")
        chunk = original_read(descriptor, size)
        if info.st_ino == original_inode and chunk and not attacked:
            attacked = True
            if attack == "truncate":
                selected.write_bytes(b"short")
            elif attack == "grow":
                with selected.open("ab") as stream:
                    stream.write(b"growth")
            elif attack == "replace":
                replacement = qmd / "replacement"
                replacement.write_bytes(b"y" * 1024)
                os.replace(replacement, selected)
        return chunk

    monkeypatch.setattr(secure_fs.os, "read", attacking_read)

    with pytest.raises(SecureTreeRetryableError):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: two unequal complete observations never produce a group result.
def test_unequal_stability_tokens_are_retryable(tmp_path, monkeypatch):
    data, config, qmd = _roots(tmp_path)
    original_snapshot = SecureTreeSnapshot.snapshot
    data_calls = 0

    def unstable_second_pass(self, *, deadline):
        nonlocal data_calls
        result = original_snapshot(self, deadline=deadline)
        if self.path == data:
            data_calls += 1
            if data_calls == 2:
                return replace(result, stability_token="f" * 64)
        return result

    monkeypatch.setattr(SecureTreeSnapshot, "snapshot", unstable_second_pass)

    with pytest.raises(SecureTreeRetryableError, match="snapshot-stability-mismatch"):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)


# Invariant: canonical-root replacement and a QMD descriptor outside data are structural.
def test_canonical_root_replacement_is_structural(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with _single_snapshot(root) as snapshot:
        root.rename(tmp_path / "old-root")
        root.mkdir()
        with pytest.raises(SecureTreeStructuralError, match="snapshot-canonical-root-replaced"):
            snapshot.validate_canonical_root()


def test_qmd_descriptor_must_be_data_root_child(tmp_path):
    data, config, _qmd = _roots(tmp_path)
    outside_qmd = tmp_path / "outside-qmd"
    outside_qmd.mkdir()

    with pytest.raises(SecureTreeStructuralError, match="snapshot-qmd-root-mismatch"):
        snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=outside_qmd)


# Invariant: the semantic digest remains the path/type/content-only external contract.
def test_semantic_qmd_digest_is_compatible(tmp_path):
    data, config, qmd = _roots(tmp_path)
    nested = qmd / "nested"
    nested.mkdir()
    (qmd / "profile.md").write_bytes(b"profile")
    (nested / "active.md").write_bytes(b"active")
    records = [
        {"path": ".", "type": "dir"},
        {"path": "nested", "type": "dir"},
        {
            "path": "nested/active.md",
            "sha256": hashlib.sha256(b"active").hexdigest(),
            "type": "file",
        },
        {
            "path": "profile.md",
            "sha256": hashlib.sha256(b"profile").hexdigest(),
            "type": "file",
        },
    ]
    expected = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()

    result = snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)

    assert result.qmd.semantic_digest == expected


# Invariant: content hashing never asks the kernel for more than 128 KiB per read.
def test_qmd_reads_use_bounded_chunks(tmp_path, monkeypatch):
    data, config, qmd = _roots(tmp_path)
    (qmd / "large.md").write_bytes(b"x" * (256 * 1024))
    original_read = secure_fs.os.read
    requested: list[int] = []

    def bounded_read(descriptor, size):
        requested.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(secure_fs.os, "read", bounded_read)

    snapshot_secure_tree_group(data_root=data, config_root=config, qmd_root=qmd)

    assert max(requested) <= 128 * 1024


# Invariant: every reviewed capacity ceiling and the shared deadline fail closed.
@pytest.mark.parametrize("ceiling", ["depth", "descendants", "total", "file", "name", "path", "deadline"])
def test_each_capacity_and_time_ceiling_fails_closed(tmp_path, ceiling):
    root = tmp_path / "root"
    root.mkdir()
    limits = SecureTreeLimits(8, 8, 64, 64)
    if ceiling == "depth":
        (root / "one" / "two").mkdir(parents=True)
        limits = replace(limits, max_depth=1)
    elif ceiling == "descendants":
        (root / "one").write_text("1")
        (root / "two").write_text("2")
        limits = replace(limits, max_descendants=1)
    elif ceiling == "total":
        (root / "one").write_bytes(b"1")
        (root / "two").write_bytes(b"2")
        limits = replace(limits, max_total_bytes=1)
    elif ceiling == "file":
        (root / "one").write_bytes(b"12")
        limits = replace(limits, max_file_bytes=1)

    with _single_snapshot(root, limits=limits) as snapshot:
        if ceiling == "name":
            with pytest.raises(SecureTreeCapacityError):
                snapshot._validate_component("x" * 256, code="snapshot-name-invalid")
        elif ceiling == "path":
            with pytest.raises(SecureTreeCapacityError):
                snapshot._relative_name("x" * 4095, "y")
        else:
            deadline = time.monotonic() - 1 if ceiling == "deadline" else time.monotonic() + 5
            expected = SecureTreeRetryableError if ceiling == "deadline" else SecureTreeCapacityError
            with pytest.raises(expected):
                snapshot.snapshot(deadline=deadline)


# Invariant: metadata-only mode opens regular files but never reads their content.
def test_metadata_only_mode_does_not_read_file_content(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "memory.md").write_text("must not be read")
    monkeypatch.setattr(secure_fs.os, "read", lambda *_args: pytest.fail("metadata scan read file content"))

    with _single_snapshot(root, content_hash=False) as snapshot:
        result = snapshot.snapshot(deadline=time.monotonic() + 5)

    assert result.semantic_digest is None
    assert result.descendants == 1


# Invariant: undecodable names are structural and cannot be omitted.
def test_invalid_utf8_name_is_structural(tmp_path):
    with pytest.raises(SecureTreeStructuralError, match="snapshot-name-invalid"):
        SecureTreeSnapshot._validate_component("invalid-\udcff", code="snapshot-name-invalid")
