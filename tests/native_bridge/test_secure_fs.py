from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from observational_memory.native_bridge.secure_fs import (
    SecureAccessError,
    SecureRoot,
    UnstableReadError,
    assert_disjoint_output,
)
from observational_memory.search.generation_store import StoreBusyError


def _root(path: Path, *, writable: bool) -> SecureRoot:
    path.mkdir(parents=True, exist_ok=True)
    return SecureRoot(path, writable=writable)


@pytest.mark.parametrize("attack", ["root", "component", "leaf"])
def test_input_symlink_is_rejected_at_every_level(tmp_path, attack):
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "memory.md").write_text("private")
    if attack == "root":
        approved.symlink_to(outside, target_is_directory=True)
        relative = "memory.md"
    else:
        approved.mkdir()
    if attack == "component":
        (approved / "project").symlink_to(outside, target_is_directory=True)
        relative = "project/memory.md"
    elif attack == "leaf":
        (approved / "memory.md").symlink_to(outside / "memory.md")
        relative = "memory.md"

    with pytest.raises(SecureAccessError):
        with SecureRoot(approved, writable=False) as root:
            root.read_bytes(relative)


def test_input_hard_link_is_rejected(tmp_path):
    approved = tmp_path / "approved"
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    approved.mkdir()
    os.link(outside, approved / "memory.md")

    with SecureRoot(approved, writable=False) as root:
        with pytest.raises(SecureAccessError, match="link count"):
            root.read_bytes("memory.md")


def test_group_writable_input_is_rejected(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    memory = approved / "memory.md"
    memory.write_text("mutable by another principal")
    memory.chmod(0o620)

    with SecureRoot(approved, writable=False) as root:
        with pytest.raises(SecureAccessError, match="group/world writable"):
            root.read_bytes("memory.md")


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "status.json",
        "ledger.jsonl",
        "generations/id/snapshot.json",
        "generations/id/documents.json",
        "generations/id/manifest.json",
        "materialized/native-memory.md",
        "index/bm25.json",
    ],
)
def test_output_leaf_symlink_is_quarantined_without_touching_target(tmp_path, relative):
    state = tmp_path / "state"
    native = tmp_path / "native.md"
    native.write_text("native remains")
    leaf = state / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.symlink_to(native)

    with SecureRoot(state, writable=True) as root:
        with root.acquire_store_transaction() as transaction:
            root.atomic_write_bytes(relative, b"safe output", transaction=transaction)

    assert native.read_text() == "native remains"
    assert leaf.read_bytes() == b"safe output"
    assert leaf.stat().st_nlink == 1
    assert leaf.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "status.json",
        "ledger.jsonl",
        "generations/id/snapshot.json",
        "generations/id/documents.json",
        "generations/id/manifest.json",
        "materialized/native-memory.md",
        "index/bm25.json",
    ],
)
def test_output_leaf_hard_link_is_quarantined_without_touching_target(tmp_path, relative):
    state = tmp_path / "state"
    native = tmp_path / "native.md"
    native.write_text("native remains")
    leaf = state / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    os.link(native, leaf)

    with SecureRoot(state, writable=True) as root:
        with root.acquire_store_transaction() as transaction:
            root.atomic_write_bytes(relative, b"safe output", transaction=transaction)

    assert native.read_text() == "native remains"
    assert leaf.read_bytes() == b"safe output"
    assert leaf.stat().st_nlink == 1


@pytest.mark.parametrize(
    "relative",
    [
        "state.json",
        "status.json",
        "ledger.jsonl",
        "generations/id/snapshot.json",
        "generations/id/documents.json",
        "generations/id/manifest.json",
        "materialized/native-memory.md",
        "index/bm25.json",
    ],
)
def test_preexisting_output_modes_are_hardened_before_read(tmp_path, relative):
    state = tmp_path / "state"
    leaf = state / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("{}")
    leaf.chmod(0o644)

    with SecureRoot(state, writable=True) as root:
        with root.acquire_store_transaction() as transaction:
            assert root.read_output_bytes(relative, transaction=transaction) == b"{}"

    assert leaf.stat().st_mode & 0o777 == 0o600


def test_foreign_owner_leaf_is_quarantined_before_read(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    leaf = state / "state.json"
    leaf.write_text('{"secret":"must not be read"}')

    with SecureRoot(state, writable=True) as root:
        original_stat = root._stat_leaf

        def foreign_stat(parent, name):
            info = original_stat(parent, name)
            if info is None:
                return None
            fields = {
                field: getattr(info, field) for field in dir(info) if field.startswith("st_") and field != "st_uid"
            }
            return SimpleNamespace(**fields, st_uid=os.getuid() + 1)

        monkeypatch.setattr(root, "_stat_leaf", foreign_stat)
        with root.acquire_store_transaction() as transaction:
            assert root.secure_output_leaf("state.json", transaction=transaction) is False

    assert not leaf.exists()


@pytest.mark.parametrize("mutation", ["truncate", "replace", "metadata", "symlink_swap"])
def test_exact_size_read_rejects_mid_read_mutation(tmp_path, monkeypatch, mutation):
    approved = tmp_path / "approved"
    approved.mkdir()
    path = approved / "memory.md"
    path.write_bytes(b"0123456789")
    path.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"abcdefghij")
    original_read = os.read
    mutated = False

    def attacking_read(fd, size):
        nonlocal mutated
        data = original_read(fd, size)
        if data and not mutated:
            mutated = True
            if mutation == "truncate":
                path.write_bytes(b"short")
            elif mutation == "replace":
                os.replace(replacement, path)
            elif mutation == "metadata":
                path.chmod(0o640)
            else:
                path.unlink()
                path.symlink_to(replacement)
        return data

    monkeypatch.setattr(os, "read", attacking_read)
    with SecureRoot(approved, writable=False) as root:
        with pytest.raises(UnstableReadError):
            root.read_bytes("memory.md")


def test_output_root_may_not_resolve_inside_native_root(tmp_path):
    native = tmp_path / "codex" / "memories"
    output = native / "bridge-output"
    output.mkdir(parents=True)

    with pytest.raises(SecureAccessError, match="must be disjoint"):
        assert_disjoint_output(output, [native])


def test_secure_root_binds_no_wait_store_transaction_without_child_lock(tmp_path):
    """Invariant: a bridge root lock is descriptor-owned and creates no child lock."""
    state = tmp_path / "state"
    with _root(state, writable=True) as root:
        with root.acquire_store_transaction():
            with pytest.raises(StoreBusyError):
                root.acquire_store_transaction()
            assert not (state / "run.lock").exists()
            assert not (state / "generation.lock").exists()
