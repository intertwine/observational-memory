from __future__ import annotations

import inspect
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from observational_memory.config import Config
from observational_memory.native_bridge import BridgePolicy, NativeMemoryBridge
from observational_memory.native_bridge.admission import AdmissionResult
from observational_memory.native_bridge.bridge import SecureBM25Backend
from observational_memory.search.generation import canonical_json_bytes
from observational_memory.search.generation_store import _require_store_transaction


def admitted() -> AdmissionResult:
    return AdmissionResult(
        admitted=True,
        pressure="normal",
        swap_used_bytes=800,
        swap_total_bytes=1000,
        reason="admitted",
    )


def rejected() -> AdmissionResult:
    return AdmissionResult(
        admitted=False,
        pressure="warning",
        swap_used_bytes=100,
        swap_total_bytes=1000,
        reason="memory pressure is warning",
    )


def bridge_fixture(tmp_path, *, policy=None, phase_hook=None):
    memory = tmp_path / "om"
    codex_home = tmp_path / "codex"
    claude_projects = tmp_path / "claude" / "projects"
    memory.mkdir(parents=True)
    codex_memories = codex_home / "memories"
    codex_memories.mkdir(parents=True)
    (codex_memories / "MEMORY.md").write_text("# Codex durable\n\nRemember the canary.")
    (codex_memories / "memory_summary.md").write_text("# Summary\n\nStable summary.")
    claude_memory = claude_projects / "project-a" / "memory"
    claude_memory.mkdir(parents=True)
    (claude_memory / "MEMORY.md").write_text("# Claude durable\n\nRemember the bridge.")
    config = Config(
        memory_dir=memory,
        env_file=tmp_path / "config" / "env",
        codex_home=codex_home,
        claude_projects_dir=claude_projects,
        search_backend="bm25",
    )
    selected_policy = policy or BridgePolicy(claude_projects=("project-a",))
    bridge = NativeMemoryBridge(
        config,
        selected_policy,
        admission_probe=admitted,
        phase_hook=phase_hook,
    )
    return bridge, config, codex_memories, claude_memory


def _read_json(path: Path):
    return json.loads(path.read_text())


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, delta):
        self.value += delta


def test_success_builds_one_secure_content_addressed_generation(tmp_path):
    """P3 contract: result and terminal receipt preserve one admission snapshot."""
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)

    result = bridge.run()

    assert result.status == "success"
    state_dir = config.memory_dir / ".native-memory-bridge"
    state = _read_json(state_dir / "state.json")
    assert state["schema"] == "om.native-memory-bridge.state.v2"
    assert state["generation_id"] == result.generation_id
    assert state["desired_state_digest"] == result.desired_state_digest
    assert state["backend_name"] == "bm25"
    assert state_dir.stat().st_mode & 0o777 == 0o700
    generation_dir = state_dir / "generations" / result.generation_id
    manifest = _read_json(generation_dir / "manifest.json")
    assert manifest["generation_id"] == result.generation_id
    assert _read_json(generation_dir / "index.json")["generation_id"] == result.generation_id
    assert _read_json(state_dir / "current-generation.json")["generation_id"] == result.generation_id
    ledger_record = json.loads((state_dir / "ledger.jsonl").read_text().splitlines()[-1])
    assert ledger_record["schema"] == "om.native-memory-bridge.ledger.v2"
    status = _read_json(state_dir / "status.json")
    expected_admission = {
        "admitted": True,
        "pressure": "normal",
        "swap_used_bytes": 800,
        "swap_total_bytes": 1000,
        "reason": "admitted",
        "resource_profile": "strict-default",
    }
    assert result.admission == admitted()
    assert status["admission"] == expected_admission
    receipt = _read_json(state_dir / "receipts" / f"{result.attempt_id}.json")
    assert receipt["admission"] == expected_admission
    assert {"lock", "publish_state", "ledger"} <= set(status["phase_durations_seconds"])
    assert status["consecutive_failure_count"] == 0
    assert status["total_failure_count"] == 0
    for path in state_dir.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600
        elif path.is_dir():
            assert path.stat().st_mode & 0o777 == 0o700


def test_bridge_generation_store_is_disjoint_from_live_search_and_native_roots(tmp_path):
    """Invariant: the isolated bridge cannot write the live BM25 store or native roots."""
    bridge, config, codex_memories, claude_memory = bridge_fixture(tmp_path)
    live_store = config.search_index_dir
    live_store.mkdir()
    live_sentinel = live_store / "operator-sentinel"
    live_sentinel.write_bytes(b"live search stays byte-identical\n")
    before = {
        live_sentinel: live_sentinel.read_bytes(),
        codex_memories / "MEMORY.md": (codex_memories / "MEMORY.md").read_bytes(),
        claude_memory / "MEMORY.md": (claude_memory / "MEMORY.md").read_bytes(),
    }

    result = bridge.run()

    assert result.status == "success"
    assert {path: path.read_bytes() for path in before} == before
    assert not (live_store / "current-generation.json").exists()
    assert (config.memory_dir / ".native-memory-bridge" / "current-generation.json").is_file()


def test_unchanged_validates_backend_but_skips_generation_materialize_and_index(tmp_path, monkeypatch):
    bridge, _config, _codex, _claude = bridge_fixture(tmp_path)
    first = bridge.run()
    assert first.status == "success"
    validated = False
    original_validate = SecureBM25Backend.validate

    def validate(self):
        nonlocal validated
        validated = True
        return original_validate(self)

    monkeypatch.setattr(SecureBM25Backend, "validate", validate)
    monkeypatch.setattr(SecureBM25Backend, "index_batch", lambda *_args: pytest.fail("index rebuilt"))

    second = bridge.run()

    assert second.status == "unchanged"
    assert validated is True
    assert second.generation_id == first.generation_id


def test_forged_persisted_index_fails_closed_and_cannot_return_unchanged(tmp_path):
    """Invariant: persisted-byte tampering is never repaired from committing memory."""
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    first = bridge.run()
    index_path = config.memory_dir / ".native-memory-bridge" / "generations" / first.generation_id / "index.json"
    forged = _read_json(index_path)
    forged["documents"][0]["content"] = "forged content"
    forged["tokenized_corpus"][0] = ["forged", "content"]
    index_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    index_path.chmod(0o600)

    rejected = bridge.run()

    assert first.status == "success"
    assert rejected.status == "failed"
    assert rejected.status != "unchanged"


@pytest.mark.parametrize("backend_name", ["qmd", "qmd-hybrid", "moss", "none", "unknown"])
def test_bridge_fixed_bm25_ignores_ordinary_search_backend(tmp_path, backend_name):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    first = bridge.run()
    config.search_backend = backend_name

    second = bridge.run()

    assert first.status == "success"
    assert second.status == "unchanged"
    assert second.generation_id == first.generation_id


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("document_schema_version", "om.search.document.v999", "failed"),
        ("materialization_schema_version", "om.materialization.v999", "failed"),
        ("index_schema_version", "om.search.index.v999", "failed"),
        ("policy_schema_version", "om.native-memory-bridge.policy.v999", "success"),
    ],
)
def test_fixed_schemas_reject_substitution_and_policy_version_changes_frontier(
    tmp_path,
    field,
    value,
    expected_status,
):
    """Invariant: BM25 schemas are fixed; a bridge policy revision is content-addressed."""
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    first = bridge.run()
    changed = {**BridgePolicy(claude_projects=("project-a",)).__dict__, field: value}
    replacement = NativeMemoryBridge(config, BridgePolicy(**changed), admission_probe=admitted)

    second = replacement.run()

    assert second.status == expected_status
    if expected_status == "success":
        assert second.desired_state_digest != first.desired_state_digest
        assert second.generation_id != first.generation_id
    else:
        pointer = _read_json(config.memory_dir / ".native-memory-bridge" / "current-generation.json")
        assert pointer["generation_id"] == first.generation_id


def test_backend_config_change_invalidates_unchanged_frontier(tmp_path, monkeypatch):
    """Invariant: caller-side backend digest substitution cannot change fixed BM25 truth."""
    from observational_memory.native_bridge import bridge as bridge_module

    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    first = bridge.run()
    monkeypatch.setattr(bridge_module, "BRIDGE_BACKEND_CONFIG_DIGEST", "f" * 64)

    second = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
        admission_probe=admitted,
    ).run()

    assert second.status == "failed"
    assert second.desired_state_digest != first.desired_state_digest


def test_busy_run_preserves_authoritative_files_byte_for_byte(tmp_path):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    assert bridge.run().status == "success"
    state_dir = config.memory_dir / ".native-memory-bridge"
    watched = [state_dir / "state.json", state_dir / "status.json", state_dir / "ledger.jsonl"]
    before = {path: path.read_bytes() for path in watched}

    with bridge._open_storage() as storage:
        with storage.acquire_store_transaction():
            result = bridge.run()

    assert result.status == "busy"
    assert result.durable_receipt_written is False
    assert {path: path.read_bytes() for path in watched} == before
    assert not (state_dir / "receipts" / f"{result.attempt_id}.json").exists()


def test_bridge_root_transaction_cannot_be_borrowed_by_another_thread(tmp_path):
    """Invariant: a same-process thread cannot borrow the bridge root owner."""
    bridge, _config, _codex, _claude = bridge_fixture(tmp_path)
    failures = []
    with bridge._open_storage() as storage:
        transaction = storage.acquire_store_transaction()

        def borrow_lock() -> None:
            try:
                _require_store_transaction(transaction, storage.path)
            except RuntimeError as exc:
                failures.append(str(exc))
            else:
                failures.append("borrowed")

        try:
            thread = threading.Thread(target=borrow_lock)
            thread.start()
            thread.join(2)
        finally:
            transaction.release()

    assert failures
    assert failures != ["borrowed"]
    assert "different thread" in failures[0]


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
@pytest.mark.parametrize("relative", ["state.json", "status.json", "ledger.jsonl", "current-generation.json"])
def test_preexisting_output_attack_cannot_write_native_memory(tmp_path, relative, attack):
    bridge, config, codex_memories, _claude = bridge_fixture(tmp_path)
    state_dir = config.memory_dir / ".native-memory-bridge"
    state_dir.mkdir()
    target = codex_memories / "do-not-write.md"
    target.write_text("native remains")
    leaf = state_dir / relative
    leaf.parent.mkdir(parents=True, exist_ok=True)
    if attack == "symlink":
        leaf.symlink_to(target)
    else:
        os.link(target, leaf)

    result = bridge.run()

    assert result.status == "success"
    assert target.read_text() == "native remains"
    assert leaf.stat().st_nlink == 1
    assert not leaf.is_symlink()


def test_failure_path_hardens_all_preexisting_state_leaves(tmp_path):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    bridge.admission_probe = rejected
    state_dir = config.memory_dir / ".native-memory-bridge"
    for relative in ("state.json", "status.json", "ledger.jsonl", "current-generation.json"):
        leaf = state_dir / relative
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("{}")
        leaf.chmod(0o644)

    result = bridge.run()

    assert result.status == "rejected"
    for path in state_dir.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600
        elif path.is_dir():
            assert path.stat().st_mode & 0o777 == 0o700
    status = _read_json(state_dir / "status.json")
    assert status["retry_at"] is not None
    assert status["status"] == "rejected"
    assert result.attempt_id == status["attempt_id"]
    receipt = _read_json(state_dir / "receipts" / f"{result.attempt_id}.json")
    assert receipt["attempt_id"] == result.attempt_id
    assert receipt["terminal"] is True


def test_invalid_0644_state_is_hardened_then_quarantined_before_normal_read(tmp_path):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    state_dir = config.memory_dir / ".native-memory-bridge"
    state_dir.mkdir()
    state = state_dir / "state.json"
    state.write_text("{not-json")
    state.chmod(0o644)

    result = bridge.run()

    assert result.status == "success"
    assert _read_json(state)["status"] == "success"
    quarantined = list((state_dir / "quarantine").iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].stat().st_mode & 0o777 == 0o600


def test_raw_memory_and_transcripts_are_hard_excluded(tmp_path):
    bridge, config, codex_memories, claude_memory = bridge_fixture(tmp_path)
    (codex_memories / "raw_memories.md").write_text("raw codex transcript")
    sessions = codex_memories / "sessions"
    sessions.mkdir()
    (sessions / "turn.md").write_text("raw codex session")
    (claude_memory / "raw_memories.md").write_text("raw claude transcript")

    result = bridge.run()

    assert result.status == "success"
    index = _read_json(
        config.memory_dir / ".native-memory-bridge" / "generations" / result.generation_id / "index.json"
    )
    serialized = json.dumps(index["documents"])
    assert "raw codex transcript" not in serialized
    assert "raw codex session" not in serialized
    assert "raw claude transcript" not in serialized
    assert "Claude durable" in serialized


def test_claude_source_requires_explicit_project_opt_in(tmp_path):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path, policy=BridgePolicy())

    result = bridge.run()

    assert result.status == "success"
    documents = _read_json(
        config.memory_dir / ".native-memory-bridge" / "generations" / result.generation_id / "index.json"
    )["documents"]
    assert not any(document["metadata"]["native_agent"] == "claude" for document in documents)


@pytest.mark.parametrize("unsafe", ["raw_memories.md", "sessions/turn.md"])
def test_codex_allowlist_cannot_override_raw_exclusions(tmp_path, unsafe):
    policy = BridgePolicy(codex_allowlist=(unsafe,))
    bridge, _config, codex_memories, _claude = bridge_fixture(tmp_path, policy=policy)
    target = codex_memories / unsafe
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("raw")

    result = bridge.run()

    assert result.status == "failed"
    assert "excluded" in result.message


@pytest.mark.parametrize(
    "event",
    [
        "before:secure_state",
        "after:secure_state",
        "before:backend_validation",
        "after:backend_validation",
        "before:stable_snapshot",
        "after:stable_snapshot",
        "before:desired_state",
        "after:desired_state",
        "before:immutable_generation",
        "after:immutable_generation",
        "before:materialization",
        "after:materialization",
        "before:index",
        "after:index",
        "before:verify_commit",
        "after:verify_commit",
        "before:publish_state",
        "after:publish_state",
        "before:ledger",
        "after:ledger",
    ],
)
def test_failure_before_and_after_every_transaction_phase_is_recoverable(tmp_path, event):
    clock = MutableClock()

    def fail(selected):
        if selected == event:
            raise RuntimeError(f"interrupted at {event}")

    bridge, config, _codex, _claude = bridge_fixture(tmp_path, phase_hook=fail)
    bridge.now = clock

    failed = bridge.run()
    clock.advance(timedelta(hours=1, seconds=1))
    recovered = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
        admission_probe=admitted,
        now=clock,
    ).run()

    assert failed.status == "failed"
    assert recovered.status in {"success", "unchanged"}
    state_dir = config.memory_dir / ".native-memory-bridge"
    pointer = _read_json(state_dir / "current-generation.json")
    assert (
        _read_json(state_dir / "generations" / pointer["generation_id"] / "index.json")["generation_id"]
        == pointer["generation_id"]
    )


def test_failure_before_pointer_does_not_advance_frontier_and_next_run_recovers(tmp_path, monkeypatch):
    """Invariant: a staged generation cannot advance the successful pointer."""
    clock = MutableClock()
    bridge, config, codex_memories, _claude = bridge_fixture(tmp_path)
    bridge.now = clock
    first = bridge.run()
    state_path = config.memory_dir / ".native-memory-bridge" / "state.json"
    first_state = state_path.read_bytes()
    pointer_path = config.memory_dir / ".native-memory-bridge" / "current-generation.json"
    first_pointer = pointer_path.read_bytes()
    (codex_memories / "MEMORY.md").write_text("# Changed\n\nA new generation.")

    def fail_before_pointer(event):
        if event == "after:staged_verify":
            raise RuntimeError("interrupted before pointer")

    from observational_memory.search import bm25 as bm25_module

    monkeypatch.setattr(bm25_module, "_publication_phase", fail_before_pointer)
    interrupted = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
        admission_probe=admitted,
        now=clock,
    ).run()

    assert interrupted.status == "failed"
    assert state_path.read_bytes() == first_state
    assert pointer_path.read_bytes() == first_pointer

    monkeypatch.setattr(bm25_module, "_publication_phase", lambda _phase: None)
    clock.advance(timedelta(hours=1, seconds=1))
    recovered = bridge.run()
    assert recovered.status == "success"
    assert recovered.generation_id != first.generation_id
    assert _read_json(state_path)["generation_id"] == recovered.generation_id


def test_outer_worker_failure_records_status_ledger_and_backoff_without_advancing_state(tmp_path):
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    assert bridge.run().status == "success"
    state_dir = config.memory_dir / ".native-memory-bridge"
    state_before = (state_dir / "state.json").read_bytes()

    result = bridge.record_external_failure(
        "outer RSS limit exceeded",
        admission=admitted(),
        telemetry={"peak_tree_rss_bytes": 512 * 1024 * 1024, "rss_sample_count": 1},
    )

    assert result.status == "failed"
    assert (state_dir / "state.json").read_bytes() == state_before
    status = _read_json(state_dir / "status.json")
    assert status["status"] == "failed"
    assert status["retry_at"] is not None
    assert status["consecutive_failure_count"] == 1
    assert status["total_failure_count"] == 1
    assert status["telemetry"]["peak_tree_rss_bytes"] == 512 * 1024 * 1024
    assert "RSS limit" in status["message"]
    ledger = [json.loads(line) for line in (state_dir / "ledger.jsonl").read_text().splitlines()]
    assert ledger[-1]["status"] == "failed"


def test_supervisor_finalizes_exact_attempt_receipts_without_latest_status_cross_talk(tmp_path):
    """Invariant: supervisor telemetry names one immutable attempt, never latest status."""
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    first_id = "1" * 32
    second_id = "2" * 32

    first = bridge.run_with_admission(admitted(), supervised=True, attempt_id=first_id)
    second = bridge.run_with_admission(admitted(), supervised=True, attempt_id=second_id)
    assert first.attempt_id == first_id
    assert second.attempt_id == second_id
    state_dir = config.memory_dir / ".native-memory-bridge"
    assert _read_json(state_dir / "status.json")["attempt_id"] == second_id

    first_telemetry = {"peak_tree_rss_bytes": 1234, "rss_sample_count": 1}
    bridge.record_supervisor_telemetry(first_id, first_telemetry)
    first_receipt = _read_json(state_dir / "receipts" / f"{first_id}.json")
    assert first_receipt["attempt_id"] == first_id
    assert first_receipt["telemetry"] == first_telemetry
    first_receipt_bytes = (state_dir / "receipts" / f"{first_id}.json").read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        bridge.record_supervisor_telemetry(
            first_id,
            {"peak_tree_rss_bytes": 9999, "rss_sample_count": 1},
        )
    assert (state_dir / "receipts" / f"{first_id}.json").read_bytes() == first_receipt_bytes
    assert not (state_dir / "receipts" / f"{second_id}.json").exists()
    latest = _read_json(state_dir / "status.json")
    assert latest["attempt_id"] == second_id
    assert latest["telemetry"] == {"peak_tree_rss_bytes": None, "rss_sample_count": 0}

    second_telemetry = {"peak_tree_rss_bytes": None, "rss_sample_count": 0}
    bridge.record_supervisor_telemetry(second_id, second_telemetry)
    assert _read_json(state_dir / "receipts" / f"{second_id}.json")["telemetry"] == second_telemetry


def test_external_failure_reports_no_receipt_while_another_attempt_owns_root(tmp_path):
    """Invariant: a busy external-failure path never writes without root ownership."""
    bridge, config, _codex, _claude = bridge_fixture(tmp_path)
    attempt_id = "e" * 32

    with bridge._open_storage() as owner:
        with owner.acquire_store_transaction():
            result = bridge.record_external_failure(
                "external isolation failure",
                admission=admitted(),
                telemetry={"peak_tree_rss_bytes": None, "rss_sample_count": 0},
                attempt_id=attempt_id,
            )

    assert result.attempt_id == attempt_id
    assert result.durable_receipt_written is False
    assert "no durable attempt receipt" in result.message
    assert not (config.memory_dir / ".native-memory-bridge" / "receipts" / f"{attempt_id}.json").exists()


def test_durable_retry_blocks_reentry_before_native_source_capture(tmp_path, monkeypatch):
    clock = MutableClock()

    def fail(event):
        if event == "before:stable_snapshot":
            raise RuntimeError("deterministic failure")

    bridge, config, _codex, _claude = bridge_fixture(tmp_path, phase_hook=fail)
    bridge.now = clock
    failed = bridge.run()
    monkeypatch.setattr(
        "observational_memory.native_bridge.bridge.capture_native_snapshot",
        lambda **_kwargs: pytest.fail("native source capture ran during durable backoff"),
    )
    retry = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
        admission_probe=admitted,
        now=clock,
    ).run()

    assert failed.status == "failed"
    assert retry.status == "deferred"
    status = _read_json(config.memory_dir / ".native-memory-bridge" / "status.json")
    assert status["consecutive_failure_count"] == 1
    assert status["total_failure_count"] == 1


def test_expired_retry_allows_one_new_attempt(tmp_path):
    clock = MutableClock()

    def fail(event):
        if event == "before:stable_snapshot":
            raise RuntimeError("deterministic failure")

    bridge, config, _codex, _claude = bridge_fixture(tmp_path, phase_hook=fail)
    bridge.now = clock
    assert bridge.run().status == "failed"
    clock.advance(timedelta(hours=1, seconds=1))

    result = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
        admission_probe=admitted,
        now=clock,
    ).run()

    assert result.status == "success"
    status = _read_json(config.memory_dir / ".native-memory-bridge" / "status.json")
    assert status["consecutive_failure_count"] == 0
    assert status["total_failure_count"] == 1


def test_corrupt_committed_generation_fails_closed_then_new_source_generation_recovers(tmp_path):
    """Invariant: incoherent published state fails closed without stale fallback."""
    clock = MutableClock()
    bridge, config, codex_memories, _claude = bridge_fixture(tmp_path)
    bridge.now = clock
    first = bridge.run()
    state_dir = config.memory_dir / ".native-memory-bridge"
    index_path = state_dir / "generations" / first.generation_id / "index.json"
    index_path.write_text("corrupt committed artifact")
    index_path.chmod(0o600)

    failed = bridge.run()
    assert failed.status == "failed"
    clock.advance(timedelta(hours=1, seconds=1))
    (codex_memories / "MEMORY.md").write_text("# Changed\n\nNew content-addressed generation.")
    recovered = bridge.run()

    assert recovered.status == "success"
    assert recovered.generation_id != first.generation_id
    assert _read_json(state_dir / "current-generation.json")["generation_id"] == recovered.generation_id


def test_bridge_module_has_no_llm_provider_reflector_or_catchup_route():
    import observational_memory.native_bridge.bridge as bridge_module
    import observational_memory.native_bridge.sources as sources_module

    source = inspect.getsource(bridge_module) + inspect.getsource(sources_module)
    forbidden = (
        "observational_memory.llm",
        "run_reflector",
        "_maybe_run_reflector_catchup",
        "observe_all_",
        "provider_jobs",
    )
    assert not any(token in source for token in forbidden)
