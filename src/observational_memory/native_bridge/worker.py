"""Strict outer wall-clock and process-tree RSS bounds for bridge runs."""

from __future__ import annotations

import copy
import math
import multiprocessing
import os
import queue
import resource
import signal
import subprocess
import time
import uuid
from dataclasses import replace
from typing import Any

from .admission import AdmissionResult
from .bridge import BridgeResult, NativeMemoryBridge
from .profiles import STRICT_DEFAULT_PROFILE

BRIDGE_TIMEOUT_SECONDS = 15
BRIDGE_MAX_RSS_BYTES = 128 * 1024 * 1024
RSS_SAMPLE_INTERVAL_SECONDS = 0.5


class BridgeWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        admission: AdmissionResult | None = None,
        telemetry: dict[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.admission = admission
        self.telemetry = telemetry or {}
        self.attempt_id = attempt_id


class BridgeWorkerTimeout(BridgeWorkerError):
    pass


class BridgeWorkerMemoryExceeded(BridgeWorkerError):
    pass


class BridgeWorkerProbeError(BridgeWorkerError):
    pass


class BridgeWorkerIsolationError(BridgeWorkerError):
    pass


def _worker_uses_supplied_admission() -> AdmissionResult:
    """Fail closed if a child tries to replace the parent's admission evidence."""
    raise BridgeWorkerIsolationError("bridge worker must use the supplied parent admission evidence")


def _bridge_for_worker(bridge: Any) -> Any:
    """Remove the parent-only admission callable from a native bridge spawn payload."""
    if not isinstance(bridge, NativeMemoryBridge):
        return bridge
    worker_bridge = copy.copy(bridge)
    worker_bridge.admission_probe = _worker_uses_supplied_admission
    return worker_bridge


def _process_table() -> dict[int, tuple[int, int]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="],
        capture_output=True,
        text=True,
        timeout=2,
    )
    if result.returncode != 0:
        raise BridgeWorkerProbeError("process-tree RSS probe failed")
    table: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, ppid, rss_kib = (int(field) for field in fields)
        except ValueError:
            continue
        table[pid] = (ppid, rss_kib * 1024)
    if not table:
        raise BridgeWorkerProbeError("process-tree RSS probe returned no processes")
    return table


def process_tree(root_pid: int) -> tuple[set[int], int]:
    """Return all live descendants plus root and their cumulative RSS."""
    table = _process_table()
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _rss) in table.items():
            if ppid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    if root_pid not in table:
        raise BridgeWorkerProbeError("bridge worker disappeared during RSS probe")
    return selected, sum(table[pid][1] for pid in selected if pid in table)


def _terminate_tree(process: multiprocessing.Process, *, attempt_id: str | None = None) -> None:
    """Immediately kill the processless worker and confirm no snapshot survivor."""
    pids: set[int] = set()
    if process.pid is not None:
        try:
            pids, _rss = process_tree(process.pid)
        except BridgeWorkerProbeError:
            pids = {process.pid}
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.join(1)
    if process.is_alive():
        raise BridgeWorkerIsolationError("bridge worker survived immediate SIGKILL", attempt_id=attempt_id)
    survivors: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            survivors.append(pid)
        else:
            survivors.append(pid)
    if survivors:
        raise BridgeWorkerIsolationError("bridge worker process snapshot has survivors", attempt_id=attempt_id)


def _set_processless_worker() -> None:
    """Make the approved BM25 worker unable to create a process."""
    if os.getuid() == 0:
        raise BridgeWorkerIsolationError("bridge worker refuses process isolation as root")
    if not hasattr(resource, "RLIMIT_NPROC"):
        raise BridgeWorkerIsolationError("RLIMIT_NPROC is unavailable")
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        applied = resource.getrlimit(resource.RLIMIT_NPROC)
    except (OSError, ValueError) as exc:
        raise BridgeWorkerIsolationError("could not apply processless worker limit") from exc
    if applied != (0, 0):
        raise BridgeWorkerIsolationError("processless worker limit readback failed")


def _worker_entry(
    result_queue: Any,
    bridge: NativeMemoryBridge,
    admission: AdmissionResult,
    attempt_id: str,
) -> None:
    try:
        _set_processless_worker()
        if isinstance(bridge, NativeMemoryBridge):
            result = bridge.run_with_admission(admission, supervised=True, attempt_id=attempt_id)
        else:
            result = bridge.run_with_admission(admission)
            if isinstance(result, BridgeResult):
                result = replace(result, attempt_id=attempt_id)
        result_queue.put(("ok", result))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def run_bounded_bridge(
    bridge: NativeMemoryBridge,
    *,
    timeout_seconds: float | None = None,
    max_rss_bytes: int | None = None,
) -> BridgeResult:
    """Probe once, then run in one spawned, processless, bounded child."""
    attempt_id = uuid.uuid4().hex
    profile = getattr(bridge, "resource_profile", STRICT_DEFAULT_PROFILE)
    if timeout_seconds is None:
        timeout_seconds = profile.timeout_seconds
    if max_rss_bytes is None:
        max_rss_bytes = profile.max_rss_bytes
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > profile.timeout_seconds:
        raise ValueError(f"{profile.name} deadline must be positive and at most {profile.timeout_seconds}s")
    if max_rss_bytes <= 0 or max_rss_bytes > profile.max_rss_bytes:
        raise ValueError(f"{profile.name} RSS limit must be positive and at most {profile.max_rss_bytes} bytes")
    outer_started = time.monotonic()
    admission_started = outer_started
    try:
        admission = bridge.admission_probe()
    except BridgeWorkerError as exc:
        if exc.attempt_id is None:
            exc.attempt_id = attempt_id
        raise
    except Exception as exc:
        raise BridgeWorkerProbeError(
            f"native-memory bridge admission probe failed: {exc}",
            attempt_id=attempt_id,
            telemetry={
                "admission_probe_seconds": round(time.monotonic() - admission_started, 6),
                "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
                "peak_tree_rss_bytes": None,
                "rss_sample_count": 0,
            },
        ) from exc
    admission_seconds = time.monotonic() - admission_started
    if not admission.admitted:
        if isinstance(bridge, NativeMemoryBridge):
            return bridge.run_with_admission(admission, attempt_id=attempt_id)
        result = bridge.run_with_admission(admission)
        return replace(result, attempt_id=attempt_id)
    deadline = outer_started + timeout_seconds
    if time.monotonic() >= deadline:
        telemetry = {
            "admission_probe_seconds": round(admission_seconds, 6),
            "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
            "peak_tree_rss_bytes": None,
            "rss_sample_count": 0,
        }
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit during admission",
            admission=admission,
            telemetry=telemetry,
            attempt_id=attempt_id,
        )

    try:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue(maxsize=1)
        worker_bridge = _bridge_for_worker(bridge)
        process = context.Process(target=_worker_entry, args=(result_queue, worker_bridge, admission, attempt_id))
        process.start()
    except Exception as exc:
        raise BridgeWorkerIsolationError(
            f"native-memory bridge worker could not start: {exc}",
            admission=admission,
            attempt_id=attempt_id,
        ) from exc
    next_rss_check = time.monotonic()
    peak_rss_bytes: int | None = None
    rss_sample_count = 0
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_tree(process, attempt_id=attempt_id)
            telemetry = {
                "admission_probe_seconds": round(admission_seconds, 6),
                "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
                "peak_tree_rss_bytes": peak_rss_bytes,
                "rss_sample_count": rss_sample_count,
            }
            raise BridgeWorkerTimeout(
                f"native-memory bridge exceeded {timeout_seconds}s outer limit",
                admission=admission,
                telemetry=telemetry,
                attempt_id=attempt_id,
            )
        process.join(min(0.1, remaining))
        if not process.is_alive():
            break
        if time.monotonic() >= next_rss_check:
            try:
                _pids, rss = process_tree(process.pid)
            except BridgeWorkerProbeError as exc:
                _terminate_tree(process, attempt_id=attempt_id)
                telemetry = {
                    "admission_probe_seconds": round(admission_seconds, 6),
                    "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
                    "peak_tree_rss_bytes": peak_rss_bytes,
                    "rss_sample_count": rss_sample_count,
                }
                raise BridgeWorkerProbeError(
                    str(exc),
                    admission=admission,
                    telemetry=telemetry,
                    attempt_id=attempt_id,
                ) from exc
            rss_sample_count += 1
            peak_rss_bytes = rss if peak_rss_bytes is None else max(peak_rss_bytes, rss)
            next_rss_check = time.monotonic() + RSS_SAMPLE_INTERVAL_SECONDS
            if rss > max_rss_bytes:
                _terminate_tree(process, attempt_id=attempt_id)
                telemetry = {
                    "admission_probe_seconds": round(admission_seconds, 6),
                    "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
                    "peak_tree_rss_bytes": peak_rss_bytes,
                    "rss_sample_count": rss_sample_count,
                }
                raise BridgeWorkerMemoryExceeded(
                    f"native-memory bridge exceeded {max_rss_bytes // (1024 * 1024)} MiB process-tree RSS",
                    admission=admission,
                    telemetry=telemetry,
                    attempt_id=attempt_id,
                )

    try:
        status, payload = result_queue.get(timeout=1)
    except queue.Empty:
        raise BridgeWorkerIsolationError(
            f"native-memory bridge child exited with status {process.exitcode}",
            admission=admission,
            attempt_id=attempt_id,
        ) from None
    if status != "ok":
        raise BridgeWorkerIsolationError(
            f"native-memory bridge child failed: {payload}",
            admission=admission,
            attempt_id=attempt_id,
        )
    if not isinstance(payload, BridgeResult):
        raise BridgeWorkerIsolationError(
            "native-memory bridge child returned an invalid result",
            admission=admission,
            attempt_id=attempt_id,
        )
    if payload.attempt_id != attempt_id:
        raise BridgeWorkerIsolationError(
            "native-memory bridge child returned a mismatched attempt ID",
            admission=admission,
            attempt_id=attempt_id,
        )
    telemetry = {
        "admission_probe_seconds": round(admission_seconds, 6),
        "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
        "peak_tree_rss_bytes": peak_rss_bytes,
        "rss_sample_count": rss_sample_count,
        "worker_process_limit": {"resource": "RLIMIT_NPROC", "soft": 0, "hard": 0},
    }
    try:
        if isinstance(bridge, NativeMemoryBridge) and payload.status != "busy":
            bridge.record_supervisor_telemetry(attempt_id, telemetry)
        elif payload.status != "busy":
            bridge.record_supervisor_telemetry(telemetry)
    except Exception as exc:
        raise BridgeWorkerIsolationError(
            f"native-memory bridge supervisor finalization failed: {exc}",
            admission=admission,
            telemetry=telemetry,
            attempt_id=attempt_id,
        ) from exc
    return payload
