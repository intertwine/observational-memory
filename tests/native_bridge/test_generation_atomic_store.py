"""Deterministic invariants for root ownership and fixed BM25 commit authority."""

from __future__ import annotations

import copy
import errno
import inspect
import json
import os
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import observational_memory.search as search_module
import observational_memory.search.bm25 as bm25_module
import observational_memory.search.generation_store as generation_store_module
from observational_memory.config import Config
from observational_memory.native_bridge.secure_fs import SecureRoot
from observational_memory.search import Document, DocumentSource, get_backend, reindex
from observational_memory.search.bm25 import BM25GenerationStore
from observational_memory.search.generation import canonical_json_bytes
from observational_memory.search.generation_store import (
    GenerationStoreError,
    StoreBusyError,
    StoreLockUnsupportedError,
    StoreReadRoot,
    StoreTransaction,
)


def _config(tmp_path, content: str = "generation alpha") -> Config:
    projects = tmp_path / "projects"
    projects.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    config = Config(memory_dir=memory, claude_projects_dir=projects, search_backend="bm25")
    config.observations_path.write_text(f"# Observations\n\n## 2026-01-01\n\n{content}\n")
    return config


def _pointer(config: Config) -> dict:
    return json.loads((config.search_index_dir / "current-generation.json").read_text())


def _current_corpus(config: Config) -> str:
    return "\n".join(document.content for document in get_backend("bm25", config)._documents)


def _external_lock_result(root) -> subprocess.CompletedProcess[str]:
    script = """
from pathlib import Path
from observational_memory.search.generation_store import StoreBusyError, StoreTransaction
try:
    StoreTransaction.acquire(Path(__import__('sys').argv[1]))
except StoreBusyError:
    raise SystemExit(75)
raise SystemExit(0)
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, environment.get("PYTHONPATH"))))
    return subprocess.run(
        [sys.executable, "-c", script, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    )


def test_public_surface_has_one_transaction_owner_and_no_supplied_batch_route(tmp_path):
    """Invariant: no public supplied-batch path can bypass root transaction ownership."""
    assert list(inspect.signature(reindex).parameters) == ["config"]
    assert not hasattr(search_module, "generation_lock")
    config = _config(tmp_path)
    backend = get_backend("bm25", config)
    with pytest.raises(RuntimeError, match="transaction owner"):
        backend.index([Document("x", DocumentSource.OBSERVATIONS, "x", "cannot publish")])


def test_fixed_authority_has_no_verifier_callback_or_schema_strategy_surface(tmp_path):
    """Regression: cycle-2 verifier substitution has no callable publication surface."""
    publish_parameters = list(inspect.signature(BM25GenerationStore.publish).parameters)
    read_parameters = list(inspect.signature(BM25GenerationStore.read_current).parameters)
    assert publish_parameters == ["self", "transaction", "immutable_batch", "bridge_metadata"]
    assert read_parameters == ["self", "transaction_or_read_root"]
    forbidden = {"verifier", "validator", "callback", "schema", "strategy", "authority"}
    assert forbidden.isdisjoint(publish_parameters)
    assert forbidden.isdisjoint(read_parameters)

    config = _config(tmp_path)
    with StoreTransaction.acquire(config.search_index_dir) as transaction:
        batch = search_module._capture_document_batch_owned(config, transaction)
        with pytest.raises(TypeError, match="unexpected keyword"):
            BM25GenerationStore(config.search_index_dir).publish(
                transaction,
                batch,
                verifier=lambda *_args: ({}, {}),  # type: ignore[call-arg]
            )
    assert not (config.search_index_dir / "current-generation.json").exists()


def test_forged_copied_released_and_cross_thread_transactions_fail_closed(tmp_path):
    """Invariant: transaction identity cannot be forged, copied, reused, or crossed."""
    config = _config(tmp_path)
    forged = object.__new__(StoreTransaction)
    with pytest.raises(RuntimeError, match="active StoreTransaction"):
        BM25GenerationStore(config.search_index_dir).publish(forged, object())  # type: ignore[arg-type]

    failures: list[str] = []
    with StoreTransaction.acquire(config.search_index_dir) as transaction:
        batch = search_module._capture_document_batch_owned(config, transaction)
        with pytest.raises(TypeError, match="copied"):
            copy.copy(transaction)
        with pytest.raises(TypeError, match="copied"):
            copy.deepcopy(transaction)
        with pytest.raises(TypeError, match="pickled"):
            pickle.dumps(transaction)

        def cross_thread_publish() -> None:
            try:
                BM25GenerationStore(config.search_index_dir).publish(transaction, batch)
            except RuntimeError as exc:
                failures.append(str(exc))

        thread = threading.Thread(target=cross_thread_publish)
        thread.start()
        thread.join(2)
        assert failures and "different thread" in failures[0]
    with pytest.raises(RuntimeError, match="released"):
        BM25GenerationStore(config.search_index_dir).publish(transaction, batch)
    assert not (config.search_index_dir / "current-generation.json").exists()


def test_same_process_second_open_and_thread_cannot_acquire_root_lock(tmp_path):
    """Invariant: macOS same-process and same-thread independent opens conflict."""
    config = _config(tmp_path)
    thread_errors: list[str] = []
    with StoreTransaction.acquire(config.search_index_dir):
        with pytest.raises(StoreBusyError):
            StoreTransaction.acquire(config.search_index_dir)

        def acquire_in_thread() -> None:
            try:
                StoreTransaction.acquire(config.search_index_dir)
            except StoreBusyError as exc:
                thread_errors.append(str(exc))

        contender = threading.Thread(target=acquire_in_thread)
        contender.start()
        contender.join(2)
        assert thread_errors
    with StoreTransaction.acquire(config.search_index_dir):
        pass


def test_nested_secure_root_rejection_preserves_live_kernel_lock(tmp_path):
    """Regression: nested acquisition cannot unlock its independent live owner."""
    config = _config(tmp_path)
    config.search_index_dir.mkdir(mode=0o700)
    with SecureRoot(config.search_index_dir, writable=True) as secure_root:
        with secure_root.acquire_store_transaction():
            with pytest.raises(StoreBusyError):
                secure_root.acquire_store_transaction()
            external = _external_lock_result(config.search_index_dir)
            assert external.returncode == 75, external.stderr
        external_after_release = _external_lock_result(config.search_index_dir)
        assert external_after_release.returncode == 0, external_after_release.stderr


def test_failed_post_flock_cleanup_cannot_disturb_independent_successor(
    tmp_path,
    monkeypatch,
):
    """Regression: an old post-unlock close cannot release its successor's lock."""
    config = _config(tmp_path)
    config.search_index_dir.mkdir(mode=0o700)
    before_unlock = threading.Event()
    allow_unlock = threading.Event()
    close_blocked = threading.Event()
    allow_close = threading.Event()
    failed_fd: list[int] = []
    failures: list[BaseException] = []
    original_validate = generation_store_module.validate_canonical_root
    original_unlock = generation_store_module._unlock
    original_close = generation_store_module.os.close
    failing_thread: threading.Thread | None = None

    def fail_first_canonical_validation(transaction):
        if failing_thread is not None and threading.current_thread() is failing_thread:
            raise GenerationStoreError("injected post-flock canonical failure")
        return original_validate(transaction)

    def controlled_unlock(fd):
        if failing_thread is not None and threading.current_thread() is failing_thread:
            failed_fd.append(fd)
            before_unlock.set()
            assert allow_unlock.wait(2)
        return original_unlock(fd)

    def controlled_close(fd):
        if (
            failing_thread is not None
            and threading.current_thread() is failing_thread
            and failed_fd
            and fd == failed_fd[0]
        ):
            close_blocked.set()
            assert allow_close.wait(2)
        return original_close(fd)

    monkeypatch.setattr(
        generation_store_module,
        "validate_canonical_root",
        fail_first_canonical_validation,
    )
    monkeypatch.setattr(generation_store_module, "_unlock", controlled_unlock)
    monkeypatch.setattr(generation_store_module.os, "close", controlled_close)

    with SecureRoot(config.search_index_dir, writable=True) as secure_root:

        def acquire_and_fail() -> None:
            try:
                secure_root.acquire_store_transaction()
            except BaseException as exc:
                failures.append(exc)

        failing_thread = threading.Thread(target=acquire_and_fail)
        failing_thread.start()
        assert before_unlock.wait(2)
        external_before_unlock = _external_lock_result(config.search_index_dir)
        assert external_before_unlock.returncode == 75, external_before_unlock.stderr

        allow_unlock.set()
        assert close_blocked.wait(2)
        successor = secure_root.acquire_store_transaction()
        try:
            allow_close.set()
            failing_thread.join(2)
            assert not failing_thread.is_alive()
            assert len(failures) == 1
            assert "injected post-flock canonical failure" in str(failures[0])
            external_after_old_close = _external_lock_result(config.search_index_dir)
            assert external_after_old_close.returncode == 75, external_after_old_close.stderr
        finally:
            allow_close.set()
            successor.release()

        external_after_successor = _external_lock_result(config.search_index_dir)
        assert external_after_successor.returncode == 0, external_after_successor.stderr


def test_second_process_cannot_acquire_root_descriptor_lock(tmp_path):
    """Invariant: a cooperating second process receives a non-blocking busy result."""
    config = _config(tmp_path)
    with StoreTransaction.acquire(config.search_index_dir):
        result = _external_lock_result(config.search_index_dir)
    assert result.returncode == 75, result.stderr


def test_recycled_numeric_thread_id_cannot_use_or_release_transaction(tmp_path, monkeypatch):
    """Regression: a new thread object cannot inherit authority from a recycled ID."""
    config = _config(tmp_path)
    transaction = StoreTransaction.acquire(config.search_index_dir)
    batch = search_module._capture_document_batch_owned(config, transaction)
    owner_thread_id = threading.get_ident()
    failures: list[str] = []

    monkeypatch.setattr(
        generation_store_module,
        "_current_thread_owner",
        lambda: (owner_thread_id, threading.current_thread()),
    )

    def replacement_thread() -> None:
        for operation in (
            lambda: BM25GenerationStore(config.search_index_dir).publish(transaction, batch),
            transaction.release,
        ):
            try:
                operation()
            except RuntimeError as exc:
                failures.append(str(exc))

    replacement = threading.Thread(target=replacement_thread)
    replacement.start()
    replacement.join(2)
    assert not replacement.is_alive()
    assert failures == [
        "StoreTransaction belongs to a different thread",
        "StoreTransaction can be released only by its owning process and thread",
    ]
    assert transaction.active is True
    transaction.release()
    assert not (config.search_index_dir / "current-generation.json").exists()


def test_cycle2_stale_lock_takeover_cannot_acquire_or_advance_pointer(tmp_path, monkeypatch):
    """Regression: replacing child lock paths cannot create a stale publisher takeover."""
    config = _config(tmp_path)
    reindex(config)
    prior = _pointer(config)["generation_id"]
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")
    ready = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def barrier(phase: str) -> None:
        if phase == "after:root_identity":
            ready.set()
            assert release.wait(2)

    monkeypatch.setattr(bm25_module, "_publication_phase", barrier)

    def publish() -> None:
        try:
            reindex(config)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    writer = threading.Thread(target=publish)
    writer.start()
    assert ready.wait(2)
    for child_name in ("generation.lock", "run.lock"):
        child = config.search_index_dir / child_name
        child.mkdir(mode=0o700)
        child.rmdir()
        child.mkdir(mode=0o700)
        child.rmdir()
    with pytest.raises(StoreBusyError):
        StoreTransaction.acquire(config.search_index_dir)
    assert _pointer(config)["generation_id"] == prior
    release.set()
    writer.join(3)
    assert errors == []
    assert _pointer(config)["generation_id"] != prior


def test_canonical_root_replacement_cannot_advance_replacement_pointer(tmp_path):
    """Invariant: a stale descriptor fails before it can change a replacement root."""
    config = _config(tmp_path)
    reindex(config)
    original_pointer = (config.search_index_dir / "current-generation.json").read_bytes()
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")
    detached = config.memory_dir / ".search-index.detached"
    with StoreTransaction.acquire(config.search_index_dir) as transaction:
        batch = search_module._capture_document_batch_owned(config, transaction)
        config.search_index_dir.rename(detached)
        config.search_index_dir.mkdir(mode=0o700)
        replacement_pointer = config.search_index_dir / "current-generation.json"
        replacement_pointer.write_bytes(b"replacement sentinel\n")
        replacement_pointer.chmod(0o600)
        with pytest.raises(GenerationStoreError, match="canonical store root"):
            BM25GenerationStore(config.search_index_dir).publish(transaction, batch)
        assert replacement_pointer.read_bytes() == b"replacement sentinel\n"
        assert (detached / "current-generation.json").read_bytes() == original_pointer


@pytest.mark.parametrize(
    ("error_number", "expected_error"),
    [
        (errno.ENOTSUP, StoreLockUnsupportedError),
        (errno.EIO, GenerationStoreError),
    ],
)
def test_failed_flock_rejects_before_capture_and_closes_independent_descriptor(
    tmp_path,
    monkeypatch,
    error_number,
    expected_error,
):
    """Invariant: failed or unsupported flock closes only its own descriptor."""
    config = _config(tmp_path)
    captured = False

    def unsupported(*_args) -> None:
        raise OSError(error_number, "failed flock")

    def forbidden_capture(*_args, **_kwargs):
        nonlocal captured
        captured = True
        raise AssertionError("capture ran")

    with monkeypatch.context() as context:
        context.setattr(generation_store_module.fcntl, "flock", unsupported)
        context.setattr(search_module, "_capture_document_batch_owned", forbidden_capture)
        with pytest.raises(expected_error):
            reindex(config)
    assert captured is False
    assert not (config.search_index_dir / "staging").exists()
    assert not (config.search_index_dir / "current-generation.json").exists()
    with StoreTransaction.acquire(config.search_index_dir):
        pass


def test_reproduced_two_success_race_is_serialized_and_each_return_has_coherent_truth(tmp_path, monkeypatch):
    """Regression: coordinated writers cannot return mixed durable generation truth."""
    config = _config(tmp_path)
    first_captured = threading.Event()
    release_first = threading.Event()
    observations: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    capture_count = 0
    capture_guard = threading.Lock()
    original_capture = search_module._capture_document_batch_owned
    original_commit = search_module._commit_document_batch_owned

    def capture_owned(selected_config, transaction):
        nonlocal capture_count
        batch = original_capture(selected_config, transaction)
        with capture_guard:
            capture_count += 1
            selected = capture_count
        if selected == 1:
            first_captured.set()
            assert release_first.wait(2)
        return batch

    def commit_owned(selected_config, batch, transaction):
        count = original_commit(selected_config, batch, transaction)
        observations.append((batch.generation_id, get_backend("bm25", selected_config).committed_generation_id))
        return count

    monkeypatch.setattr(search_module, "_capture_document_batch_owned", capture_owned)
    monkeypatch.setattr(search_module, "_commit_document_batch_owned", commit_owned)

    def run() -> None:
        try:
            reindex(config)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=run)
    first.start()
    assert first_captured.wait(2)
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")
    second = threading.Thread(target=run)
    second.start()
    time.sleep(0.05)
    assert capture_count == 1
    release_first.set()
    first.join(3)
    second.join(3)

    assert errors == []
    assert len(observations) == 2
    assert observations[0][0] == observations[0][1]
    assert observations[1][0] == observations[1][1]
    assert observations[0][0] != observations[1][0]
    assert _pointer(config)["generation_id"] == observations[1][0]
    assert "generation beta" in _current_corpus(config)


def test_reader_during_pointer_flip_sees_prior_then_new_complete_generation(tmp_path, monkeypatch):
    """Invariant: a concurrent reader sees one complete generation, never mixed files."""
    config = _config(tmp_path)
    reindex(config)
    prior = _pointer(config)["generation_id"]
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")
    staged = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def barrier(phase: str) -> None:
        if phase == "after:generations_flush":
            staged.set()
            assert release.wait(2)

    monkeypatch.setattr(bm25_module, "_publication_phase", barrier)

    def run() -> None:
        try:
            reindex(config)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    writer = threading.Thread(target=run)
    writer.start()
    assert staged.wait(2)
    assert get_backend("bm25", config).committed_generation_id == prior
    assert "generation alpha" in _current_corpus(config)
    release.set()
    writer.join(3)
    assert errors == []
    assert get_backend("bm25", config).committed_generation_id != prior
    assert "generation beta" in _current_corpus(config)


def test_atomic_pointer_replacement_overlap_returns_old_or_new_generation(tmp_path, monkeypatch):
    """Invariant: an exact regular-pointer flip resolves to old or new, not mixed."""
    config = _config(tmp_path)
    reindex(config)
    pointer_path = config.search_index_dir / "current-generation.json"
    old_pointer = pointer_path.read_bytes()
    old_generation = json.loads(old_pointer)["generation_id"]
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")
    reindex(config)
    new_pointer = pointer_path.read_bytes()
    new_generation = json.loads(new_pointer)["generation_id"]
    pointer_path.write_bytes(old_pointer)
    pointer_path.chmod(0o600)
    original_open = generation_store_module.os.open
    replaced = False

    def overlapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "current-generation.json" and dir_fd is not None and not replaced:
            replaced = True
            temp = config.search_index_dir / ".overlap-pointer"
            temp.write_bytes(new_pointer)
            temp.chmod(0o600)
            temp.replace(pointer_path)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(generation_store_module.os, "open", overlapping_open)
    with StoreReadRoot.open(config.search_index_dir) as read_root:
        verified = BM25GenerationStore(config.search_index_dir).read_current(read_root)
    assert replaced is True
    assert verified.generation_id in {old_generation, new_generation}


def test_malformed_pointer_churn_fails_closed(tmp_path, monkeypatch):
    """Invariant: pointer retry never makes a malformed replacement authoritative."""
    config = _config(tmp_path)
    reindex(config)
    pointer_path = config.search_index_dir / "current-generation.json"
    original_open = generation_store_module.os.open
    replaced = False

    def overlapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "current-generation.json" and dir_fd is not None and not replaced:
            replaced = True
            temp = config.search_index_dir / ".malformed-pointer"
            temp.write_bytes(b"{malformed\n")
            temp.chmod(0o600)
            temp.replace(pointer_path)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(generation_store_module.os, "open", overlapping_open)
    with StoreReadRoot.open(config.search_index_dir) as read_root:
        with pytest.raises(GenerationStoreError, match="pointer is malformed"):
            BM25GenerationStore(config.search_index_dir).read_current(read_root)


@pytest.mark.parametrize(
    "crash_phase",
    [
        "after:index_write",
        "after:manifest_write",
        "after:file_flush",
        "after:staging_flush",
        "after:staged_verify",
        "after:generation_rename",
        "after:generations_flush",
        "after:pointer_temp_write",
        "after:root_identity",
    ],
)
def test_each_crash_before_pointer_preserves_prior_generation(tmp_path, monkeypatch, crash_phase):
    """Invariant: every injected pre-pointer crash preserves prior authority."""
    config = _config(tmp_path)
    reindex(config)
    pointer_path = config.search_index_dir / "current-generation.json"
    prior_pointer = pointer_path.read_bytes()
    prior_generation = _pointer(config)["generation_id"]
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")

    def crash(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"crash at {crash_phase}")

    monkeypatch.setattr(bm25_module, "_publication_phase", crash)
    with pytest.raises(RuntimeError, match="crash at"):
        reindex(config)
    assert pointer_path.read_bytes() == prior_pointer
    assert get_backend("bm25", config).committed_generation_id == prior_generation
    assert "generation alpha" in _current_corpus(config)


def test_crash_after_pointer_exposes_new_complete_verified_generation(tmp_path, monkeypatch):
    """Invariant: a completed pointer replacement exposes one complete generation."""
    config = _config(tmp_path)
    reindex(config)
    prior = _pointer(config)["generation_id"]
    config.observations_path.write_text("# Observations\n\n## 2026-01-01\n\ngeneration beta\n")

    def crash(phase: str) -> None:
        if phase == "after:pointer_replace":
            raise RuntimeError("crash after pointer")

    monkeypatch.setattr(bm25_module, "_publication_phase", crash)
    with pytest.raises(RuntimeError, match="crash after pointer"):
        reindex(config)
    assert _pointer(config)["generation_id"] != prior
    assert "generation beta" in _current_corpus(config)


def test_persisted_index_or_manifest_tamper_fails_fixed_reader(tmp_path):
    """Invariant: fresh fixed verification rejects persisted-byte substitution."""
    config = _config(tmp_path)
    reindex(config)
    generation = config.search_index_dir / "generations" / _pointer(config)["generation_id"]
    index_path = generation / "index.json"
    index = json.loads(index_path.read_text())
    index["schema"] = "attacker.schema"
    index_path.write_bytes(canonical_json_bytes(index) + b"\n")
    index_path.chmod(0o600)
    with pytest.raises(GenerationStoreError, match="index schema"):
        get_backend("bm25", config)


def test_invalid_generation_state_fails_closed_and_preserves_legacy_bytes(tmp_path):
    """Invariants: pointer corruption fails closed and legacy bytes remain unchanged."""
    config = _config(tmp_path)
    config.search_index_dir.mkdir()
    legacy_path = config.search_index_dir / "bm25.pkl"
    legacy_document = Document("legacy", DocumentSource.OBSERVATIONS, "legacy", "legacy sentinel")
    legacy_bytes = pickle.dumps({"documents": [legacy_document], "tokenized_corpus": [["legacy", "sentinel"]]})
    legacy_path.write_bytes(legacy_bytes)
    assert reindex(config) == 1
    pointer_path = config.search_index_dir / "current-generation.json"
    pointer_path.write_bytes(canonical_json_bytes({"schema": "invalid"}) + b"\n")
    pointer_path.chmod(0o600)
    with pytest.raises(GenerationStoreError):
        get_backend("bm25", config)
    assert legacy_path.read_bytes() == legacy_bytes


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_unsafe_current_pointer_fails_closed_without_reading_target(tmp_path, attack):
    """Invariant: unsafe pointer leaves never resolve or trigger legacy fallback."""
    config = _config(tmp_path)
    reindex(config)
    pointer_path = config.search_index_dir / "current-generation.json"
    pointer_path.unlink()
    target = tmp_path / "operator-target"
    target.write_bytes(b"do not read or modify\n")
    if attack == "symlink":
        pointer_path.symlink_to(target)
    else:
        pointer_path.hardlink_to(target)
    with pytest.raises(GenerationStoreError):
        get_backend("bm25", config)
    assert target.read_bytes() == b"do not read or modify\n"


def test_first_generation_keeps_existing_legacy_pickle_byte_identical(tmp_path):
    """Invariant: generation publication never deletes or rewrites legacy bm25.pkl."""
    config = _config(tmp_path)
    config.search_index_dir.mkdir()
    legacy_path = config.search_index_dir / "bm25.pkl"
    legacy_bytes = b"legacy bytes are rollback evidence\n"
    legacy_path.write_bytes(legacy_bytes)
    reindex(config)
    assert legacy_path.read_bytes() == legacy_bytes
    assert (config.search_index_dir / "current-generation.json").is_file()


def test_legacy_reader_is_one_way_before_generation_state(tmp_path):
    """Invariant: legacy fallback never masks partial or invalid generation state."""
    config = _config(tmp_path)
    config.search_index_dir.mkdir()
    legacy_path = config.search_index_dir / "bm25.pkl"
    legacy_document = Document("legacy", DocumentSource.OBSERVATIONS, "legacy", "legacy sentinel")
    legacy_path.write_bytes(
        pickle.dumps({"documents": [legacy_document], "tokenized_corpus": [["legacy", "sentinel"]]})
    )
    assert get_backend("bm25", config).is_ready()
    (config.search_index_dir / "staging").mkdir(mode=0o700)
    with pytest.raises(GenerationStoreError, match="generation store"):
        get_backend("bm25", config)
