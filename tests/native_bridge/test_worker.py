from __future__ import annotations

import json
import os
import pickle
import resource
import signal
import subprocess
import time
from functools import partial
from pathlib import Path

import pytest

import observational_memory.native_bridge.bridge as bridge_module
from observational_memory.cli import _observer_worker_max_rss_bytes, _observer_worker_timeout_seconds
from observational_memory.config import Config
from observational_memory.native_bridge import BridgePolicy, NativeMemoryBridge
from observational_memory.native_bridge.admission import AdmissionResult
from observational_memory.native_bridge.bridge import BridgeResult
from observational_memory.native_bridge.worker import (
    BRIDGE_MAX_RSS_BYTES,
    BRIDGE_TIMEOUT_SECONDS,
    BridgeWorkerIsolationError,
    BridgeWorkerMemoryExceeded,
    BridgeWorkerProbeError,
    BridgeWorkerTimeout,
    process_tree,
    run_bounded_bridge,
)


class WorkerBridge:
    def __init__(self):
        self.telemetry = None
        self.admission_calls = 0

    def admission_probe(self):
        self.admission_calls += 1
        return AdmissionResult(True, "normal", 1, 100, "admitted")

    def run_with_admission(self, admission):
        assert admission.admitted is True
        return self.run()

    def record_supervisor_telemetry(self, telemetry):
        self.telemetry = telemetry


class SlowBridge(WorkerBridge):
    def run(self):
        time.sleep(5)
        return BridgeResult("success", 0, "late", AdmissionResult(True, "normal", 1, 100, "admitted"))


class WaitingBridge(WorkerBridge):
    def run(self):
        time.sleep(2)
        return BridgeResult("success", 0, "late", AdmissionResult(True, "normal", 1, 100, "admitted"))


class FastBridge(WorkerBridge):
    def run(self):
        return BridgeResult("success", 0, "fast", AdmissionResult(True, "normal", 1, 100, "admitted"))


class SlowAdmissionBridge(FastBridge):
    def admission_probe(self):
        time.sleep(2)
        return super().admission_probe()


class SlowFinalizationBridge(FastBridge):
    def record_supervisor_telemetry(self, telemetry):
        time.sleep(2)
        super().record_supervisor_telemetry(telemetry)


class ProcesslessProbeBridge(WorkerBridge):
    def run(self):
        limits = resource.getrlimit(resource.RLIMIT_NPROC)
        fork_blocked = False
        spawn_blocked = False
        try:
            pid = os.fork()
        except OSError:
            fork_blocked = True
        else:
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
        try:
            subprocess.run(["/usr/bin/true"], check=True)
        except OSError:
            spawn_blocked = True
        return BridgeResult(
            "success",
            0,
            (
                f"limits={limits};fork_blocked={fork_blocked};"
                f"spawn_blocked={spawn_blocked};admission_calls={self.admission_calls}"
            ),
            AdmissionResult(True, "normal", 1, 100, "admitted"),
        )


class SignalEscapeBridge(WorkerBridge):
    def __init__(self, sentinel):
        super().__init__()
        self.sentinel = sentinel

    def run(self):
        def escape(_signum, _frame):
            try:
                pid = os.fork()
            except OSError:
                return
            if pid == 0:
                os.setsid()
                self.sentinel.write_text("escaped")
                os._exit(0)

        signal.signal(signal.SIGTERM, escape)
        time.sleep(5)
        return BridgeResult("success", 0, "late", AdmissionResult(True, "normal", 1, 100, "admitted"))


def _record_strict_admission(probe_log: Path, profile) -> AdmissionResult:
    with probe_log.open("a") as stream:
        stream.write("probe\n")
    return AdmissionResult(True, "normal", 1, 100, "admitted", profile.name)


def test_outer_limits_are_exactly_fifteen_seconds_and_at_most_128_mib():
    assert BRIDGE_TIMEOUT_SECONDS == 15
    assert BRIDGE_MAX_RSS_BYTES <= 128 * 1024 * 1024


def test_legacy_observer_safeguards_remain_300_seconds_and_4096_mib(monkeypatch):
    monkeypatch.delenv("OM_OBSERVER_WORKER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OM_OBSERVER_WORKER_MAX_RSS_MB", raising=False)

    assert _observer_worker_timeout_seconds() == 300
    assert _observer_worker_max_rss_bytes() == 4096 * 1024 * 1024


def test_process_tree_rss_includes_worker_and_worker_descendants_only(monkeypatch):
    output = """\
100 1 100
101 100 200
102 101 300
103 1 400
200 1 900
"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
    )

    pids, rss = process_tree(100, timeout_seconds=0.5)

    assert pids == {100, 101, 102}
    assert rss == (100 + 200 + 300) * 1024


def test_outer_timeout_terminates_worker_tree(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    class AllPids(set):
        def __contains__(self, _item):
            return True

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda pid, *, timeout_seconds: (AllPids({pid}), 1),
    )
    with pytest.raises(BridgeWorkerTimeout, match="exceeded 1s") as caught:
        run_bounded_bridge(SlowBridge(), timeout_seconds=1)
    assert caught.value.attempt_id is not None
    assert len(caught.value.attempt_id) == 32


def test_outer_deadline_interrupts_admission_probe():
    started = time.monotonic()
    with pytest.raises(BridgeWorkerTimeout, match="during admission"):
        run_bounded_bridge(SlowAdmissionBridge(), timeout_seconds=0.1)
    assert time.monotonic() - started < 0.75


def test_outer_deadline_includes_receipt_and_telemetry_finalization():
    started = time.monotonic()
    with pytest.raises(BridgeWorkerTimeout, match="receipt and telemetry finalization"):
        run_bounded_bridge(SlowFinalizationBridge(), timeout_seconds=0.75)
    assert time.monotonic() - started < 1.25


def test_process_tree_rss_limit_terminates_worker(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    class AllPids(set):
        def __contains__(self, _item):
            return True

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda pid, *, timeout_seconds: (AllPids({pid}), 1024),
    )
    with pytest.raises(BridgeWorkerMemoryExceeded, match="exceeded 0 MiB"):
        run_bounded_bridge(WaitingBridge(), timeout_seconds=5, max_rss_bytes=1)


def test_fast_worker_cannot_evade_kernel_high_water_evidence(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    class AllPids(set):
        def __contains__(self, _item):
            return True

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda pid, *, timeout_seconds: (AllPids({pid}), 1),
    )
    with pytest.raises(BridgeWorkerMemoryExceeded, match="kernel RSS high-water") as caught:
        run_bounded_bridge(FastBridge(), timeout_seconds=5, max_rss_bytes=1)
    assert caught.value.telemetry["worker_high_water_rss_bytes"] > 1


def test_process_tree_probe_error_fails_closed(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda _pid, *, timeout_seconds: (_ for _ in ()).throw(BridgeWorkerProbeError("probe failed")),
    )
    with pytest.raises(BridgeWorkerProbeError, match="probe failed"):
        run_bounded_bridge(WaitingBridge(), timeout_seconds=5)


def test_process_tree_probe_timeout_is_capped_by_remaining_outer_deadline(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    probes: list[tuple[int, float]] = []

    def timed_out(pid, *, timeout_seconds):
        probes.append((pid, timeout_seconds))
        raise BridgeWorkerProbeError("probe timed out")

    monkeypatch.setattr(worker, "process_tree", timed_out)

    with pytest.raises(BridgeWorkerProbeError, match="probe timed out"):
        run_bounded_bridge(WaitingBridge(), timeout_seconds=0.5)

    assert probes
    assert probes[0][0] != os.getpid()
    assert all(0 < timeout <= 0.5 for _pid, timeout in probes)


def test_worker_reads_back_zero_process_limit_and_cannot_fork_or_spawn():
    bridge = ProcesslessProbeBridge()

    result = run_bounded_bridge(bridge, timeout_seconds=5)

    assert "limits=(0, 0)" in result.message
    assert "fork_blocked=True" in result.message
    assert "spawn_blocked=True" in result.message
    assert "admission_calls=1" in result.message
    assert bridge.admission_calls == 1
    assert bridge.telemetry["worker_process_limit"] == {
        "resource": "RLIMIT_NPROC",
        "soft": 0,
        "hard": 0,
    }


def test_fast_worker_acceptance_keeps_independent_kernel_high_water_evidence(monkeypatch):
    """Invariant: every accepted fast worker reports its kernel high-water mark."""
    import observational_memory.native_bridge.worker as worker

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda pid, *, timeout_seconds: ({pid}, 1),
    )
    bridge = FastBridge()

    result = run_bounded_bridge(bridge, timeout_seconds=5)

    assert result.status == "success"
    assert bridge.telemetry["worker_high_water_rss_bytes"] > 0
    assert bridge.telemetry["rss_evidence"] == ("worker process-tree samples plus independent worker kernel high-water")


def test_timeout_uses_uncatchable_kill_and_leaves_no_escape_sentinel(tmp_path, monkeypatch):
    import observational_memory.native_bridge.worker as worker

    class AllPids(set):
        def __contains__(self, _item):
            return True

    monkeypatch.setattr(
        worker,
        "process_tree",
        lambda pid, *, timeout_seconds: (AllPids({pid}), 1),
    )
    sentinel = tmp_path / "escaped"

    with pytest.raises(BridgeWorkerTimeout):
        run_bounded_bridge(SignalEscapeBridge(sentinel), timeout_seconds=1)

    assert not sentinel.exists()


def test_processless_worker_rejects_root(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    monkeypatch.setattr(worker.os, "getuid", lambda: 0)

    with pytest.raises(BridgeWorkerIsolationError, match="as root"):
        worker._set_processless_worker()


def test_processless_worker_rejects_limit_failure(monkeypatch):
    import observational_memory.native_bridge.worker as worker

    monkeypatch.setattr(worker.os, "getuid", lambda: 501)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *_args: (_ for _ in ()).throw(OSError("denied")))

    with pytest.raises(BridgeWorkerIsolationError, match="could not apply"):
        worker._set_processless_worker()


def test_native_bridge_spawn_succeeds_and_persists_supervisor_telemetry(tmp_path, monkeypatch):
    memory = tmp_path / "om"
    codex_home = tmp_path / "codex"
    codex_memories = codex_home / "memories"
    claude_projects = tmp_path / "claude"
    memory.mkdir()
    codex_memories.mkdir(parents=True)
    claude_projects.mkdir()
    (codex_memories / "MEMORY.md").write_text("# Durable\n\nProcessless bridge.")
    config = Config(
        memory_dir=memory,
        env_file=tmp_path / "config" / "env",
        codex_home=codex_home,
        claude_projects_dir=claude_projects,
        search_backend="bm25",
    )
    probe_log = tmp_path / "admission-probes.log"
    monkeypatch.setattr(bridge_module, "probe_admission", partial(_record_strict_admission, probe_log))
    bridge = NativeMemoryBridge(config, BridgePolicy())

    result = run_bounded_bridge(bridge, timeout_seconds=5)

    assert result.status == "success"
    assert probe_log.read_text() == "probe\n"
    status = json.loads((memory / ".native-memory-bridge" / "status.json").read_text())
    attempt_id = result.attempt_id
    assert attempt_id is not None
    assert len(attempt_id) == 32
    assert all(character in "0123456789abcdef" for character in attempt_id)
    assert status["attempt_id"] == attempt_id
    # The attempt tree and worker kernel high-water remain separate evidence.
    sample_count = status["telemetry"]["rss_sample_count"]
    peak = status["telemetry"]["peak_tree_rss_bytes"]
    assert (sample_count == 0) == (peak is None)
    assert status["telemetry"]["worker_high_water_rss_bytes"] > 0
    preference = status["telemetry"]["worker_memory_preference"]
    assert preference["requested_bytes"] == BRIDGE_MAX_RSS_BYTES
    assert "supervisor-and-kernel-high-water" in preference["enforcement"]
    assert status["telemetry"]["worker_process_limit"] == {
        "resource": "RLIMIT_NPROC",
        "soft": 0,
        "hard": 0,
    }
    receipt = json.loads((memory / ".native-memory-bridge" / "receipts" / f"{attempt_id}.json").read_text())
    assert receipt["attempt_id"] == attempt_id
    assert receipt["terminal"] is True
    assert receipt["telemetry"] == status["telemetry"]
    monkeypatch.undo()
    pickle.dumps(NativeMemoryBridge(config, BridgePolicy()))


def test_supervised_busy_attempt_writes_no_receipt_without_root_ownership(tmp_path):
    """Invariant: a supervised busy attempt states that durable evidence was not written."""
    memory = tmp_path / "om"
    codex_home = tmp_path / "codex"
    (codex_home / "memories").mkdir(parents=True)
    (tmp_path / "claude").mkdir()
    memory.mkdir()
    config = Config(
        memory_dir=memory,
        env_file=tmp_path / "config" / "env",
        codex_home=codex_home,
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    bridge = NativeMemoryBridge(config, BridgePolicy(), admission_probe=WorkerBridge().admission_probe)

    with bridge._open_storage() as owner:
        with owner.acquire_store_transaction():
            result = run_bounded_bridge(bridge, timeout_seconds=5)

    assert result.status == "busy"
    assert result.durable_receipt_written is False
    state_dir = memory / ".native-memory-bridge"
    assert "no durable attempt receipt" in result.message
    assert not (state_dir / "receipts" / f"{result.attempt_id}.json").exists()
    assert not (state_dir / "status.json").exists()
