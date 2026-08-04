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
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from observational_memory.search.bm25 import supervised_publication_budget

from .admission import AdmissionResult
from .bridge import BridgeResult, NativeMemoryBridge
from .profiles import STRICT_DEFAULT_PROFILE

BRIDGE_TIMEOUT_SECONDS = 15
BRIDGE_MAX_RSS_BYTES = 128 * 1024 * 1024
RSS_SAMPLE_INTERVAL_SECONDS = 0.1


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


class _DeadlineExpired(RuntimeError):
    pass


@contextmanager
def _deadline_alarm(deadline: float, phase: str):
    """Interrupt parent-only admission/finalization at one monotonic deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExpired(phase)
    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
        or not hasattr(signal, "ITIMER_REAL")
    ):
        raise BridgeWorkerIsolationError("strict bridge deadline requires the main thread and ITIMER_REAL")
    prior_timer = signal.getitimer(signal.ITIMER_REAL)
    if prior_timer != (0.0, 0.0):
        raise BridgeWorkerIsolationError("strict bridge deadline cannot replace an active process timer")
    prior_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signum, _frame) -> None:
        raise _DeadlineExpired(phase)

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)


def _max_rss_bytes() -> int:
    """Return the kernel-maintained lifetime RSS high-water mark in bytes."""
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _apply_worker_memory_preference(max_rss_bytes: int) -> dict[str, Any]:
    """Apply/read back the strongest portable per-process RSS preference.

    Darwin aliases RLIMIT_RSS to RLIMIT_AS and does not promise a hard kill.
    The supervisor therefore also kills sampled breaches and rejects the
    child's kernel high-water mark before accepting a result.
    """
    if not hasattr(resource, "RLIMIT_RSS"):
        return {
            "resource": "RLIMIT_RSS",
            "requested_bytes": max_rss_bytes,
            "applied": False,
            "enforcement": "supervisor-plus-kernel-high-water",
            "readback": None,
        }
    selected = resource.RLIMIT_RSS
    before = resource.getrlimit(selected)
    error_type: str | None = None
    try:
        hard = before[1]
        requested_soft = max_rss_bytes if hard == resource.RLIM_INFINITY else min(max_rss_bytes, hard)
        resource.setrlimit(selected, (requested_soft, hard))
    except (OSError, ValueError) as exc:
        error_type = type(exc).__name__
    after = resource.getrlimit(selected)
    applied = after[0] != resource.RLIM_INFINITY and after[0] <= max_rss_bytes
    evidence = {
        "resource": "RLIMIT_RSS",
        "requested_bytes": max_rss_bytes,
        "applied": applied,
        "enforcement": (
            "advisory-macos-plus-supervisor-and-kernel-high-water"
            if sys.platform == "darwin"
            else "rlimit-plus-supervisor-and-kernel-high-water"
        ),
        "readback": {"soft": after[0], "hard": after[1]},
    }
    if error_type is not None:
        evidence["apply_error"] = error_type
    return evidence


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
    max_rss_bytes: int,
    publication_deadline: float,
) -> None:
    memory_preference: dict[str, Any] | None = None
    try:
        _set_processless_worker()
        memory_preference = _apply_worker_memory_preference(max_rss_bytes)
        with supervised_publication_budget(
            deadline=publication_deadline,
            max_rss_bytes=max_rss_bytes,
        ):
            if isinstance(bridge, NativeMemoryBridge):
                result = bridge.run_with_admission(admission, supervised=True, attempt_id=attempt_id)
            else:
                result = bridge.run_with_admission(admission)
                if isinstance(result, BridgeResult):
                    result = replace(result, attempt_id=attempt_id)
        peak_rss_bytes = _max_rss_bytes()
        if peak_rss_bytes > max_rss_bytes:
            result_queue.put(
                (
                    "memory",
                    "worker kernel high-water mark exceeded its RSS ceiling",
                    peak_rss_bytes,
                    memory_preference,
                )
            )
            return
        result_queue.put(("ok", result, peak_rss_bytes, memory_preference))
    except BaseException as exc:
        try:
            peak_rss_bytes = _max_rss_bytes()
        except BaseException:
            peak_rss_bytes = None
        result_queue.put(
            (
                "error",
                f"{type(exc).__name__}: {exc}",
                peak_rss_bytes,
                memory_preference,
            )
        )


def run_bounded_bridge(
    bridge: NativeMemoryBridge,
    *,
    timeout_seconds: float | None = None,
    max_rss_bytes: int | None = None,
) -> BridgeResult:
    """Probe once and finish all worker evidence before one outer deadline."""
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
    deadline = outer_started + timeout_seconds
    finalization_reserve_seconds = min(1.0, timeout_seconds * 0.2)
    publication_deadline = deadline - finalization_reserve_seconds
    admission_started = outer_started
    peak_rss_bytes: int | None = None
    rss_sample_count = 0
    memory_preference: dict[str, Any] | None = None

    def telemetry(admission_seconds: float) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "admission_probe_seconds": round(admission_seconds, 6),
            "outer_duration_seconds": round(time.monotonic() - outer_started, 6),
            "outer_deadline_seconds": timeout_seconds,
            "peak_tree_rss_bytes": peak_rss_bytes,
            "rss_sample_count": rss_sample_count,
            "rss_evidence": "supervisor samples plus child kernel high-water",
            "worker_process_limit": {"resource": "RLIMIT_NPROC", "soft": 0, "hard": 0},
        }
        if memory_preference is not None:
            evidence["worker_memory_preference"] = memory_preference
        return evidence

    try:
        with _deadline_alarm(deadline, "admission"):
            admission = bridge.admission_probe()
    except _DeadlineExpired as exc:
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit during {exc}",
            telemetry=telemetry(time.monotonic() - admission_started),
            attempt_id=attempt_id,
        ) from None
    except BridgeWorkerError as exc:
        if exc.attempt_id is None:
            exc.attempt_id = attempt_id
        raise
    except Exception as exc:
        raise BridgeWorkerProbeError(
            f"native-memory bridge admission probe failed: {exc}",
            attempt_id=attempt_id,
            telemetry=telemetry(time.monotonic() - admission_started),
        ) from exc
    admission_seconds = time.monotonic() - admission_started
    if not admission.admitted:
        try:
            with _deadline_alarm(deadline, "rejected-admission receipt finalization"):
                if isinstance(bridge, NativeMemoryBridge):
                    return bridge.run_with_admission(admission, attempt_id=attempt_id)
                result = bridge.run_with_admission(admission)
        except _DeadlineExpired as exc:
            raise BridgeWorkerTimeout(
                f"native-memory bridge exceeded {timeout_seconds}s outer limit during {exc}",
                admission=admission,
                telemetry=telemetry(admission_seconds),
                attempt_id=attempt_id,
            ) from None
        return replace(result, attempt_id=attempt_id)
    if time.monotonic() >= publication_deadline:
        raise BridgeWorkerTimeout(
            "native-memory bridge exhausted its worker budget during admission",
            admission=admission,
            telemetry=telemetry(admission_seconds),
            attempt_id=attempt_id,
        )

    process: multiprocessing.Process | None = None
    try:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue(maxsize=1)
        worker_bridge = _bridge_for_worker(bridge)
        process = context.Process(
            target=_worker_entry,
            args=(
                result_queue,
                worker_bridge,
                admission,
                attempt_id,
                max_rss_bytes,
                publication_deadline,
            ),
        )
        with _deadline_alarm(deadline, "worker start"):
            process.start()
    except _DeadlineExpired as exc:
        if process is not None and process.pid is not None:
            _terminate_tree(process, attempt_id=attempt_id)
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit during {exc}",
            admission=admission,
            telemetry=telemetry(admission_seconds),
            attempt_id=attempt_id,
        ) from None
    except Exception as exc:
        raise BridgeWorkerIsolationError(
            f"native-memory bridge worker could not start: {exc}",
            admission=admission,
            attempt_id=attempt_id,
        ) from exc
    next_rss_check = time.monotonic()
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_tree(process, attempt_id=attempt_id)
            raise BridgeWorkerTimeout(
                f"native-memory bridge exceeded {timeout_seconds}s outer limit",
                admission=admission,
                telemetry=telemetry(admission_seconds),
                attempt_id=attempt_id,
            )
        if time.monotonic() >= next_rss_check:
            try:
                _pids, rss = process_tree(process.pid)
            except BridgeWorkerProbeError as exc:
                if not process.is_alive():
                    break
                _terminate_tree(process, attempt_id=attempt_id)
                raise BridgeWorkerProbeError(
                    str(exc),
                    admission=admission,
                    telemetry=telemetry(admission_seconds),
                    attempt_id=attempt_id,
                ) from exc
            rss_sample_count += 1
            peak_rss_bytes = rss if peak_rss_bytes is None else max(peak_rss_bytes, rss)
            next_rss_check = time.monotonic() + RSS_SAMPLE_INTERVAL_SECONDS
            if rss > max_rss_bytes:
                _terminate_tree(process, attempt_id=attempt_id)
                raise BridgeWorkerMemoryExceeded(
                    f"native-memory bridge exceeded {max_rss_bytes // (1024 * 1024)} MiB process-tree RSS",
                    admission=admission,
                    telemetry=telemetry(admission_seconds),
                    attempt_id=attempt_id,
                )
        process.join(min(0.025, remaining))

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit before worker result",
            admission=admission,
            telemetry=telemetry(admission_seconds),
            attempt_id=attempt_id,
        )
    try:
        status, payload, child_peak_rss_bytes, memory_preference = result_queue.get(timeout=remaining)
    except queue.Empty:
        if time.monotonic() >= deadline:
            raise BridgeWorkerTimeout(
                f"native-memory bridge exceeded {timeout_seconds}s outer limit waiting for worker result",
                admission=admission,
                telemetry=telemetry(admission_seconds),
                attempt_id=attempt_id,
            ) from None
        raise BridgeWorkerIsolationError(
            f"native-memory bridge child exited with status {process.exitcode}",
            admission=admission,
            attempt_id=attempt_id,
        ) from None
    if not isinstance(child_peak_rss_bytes, int) or child_peak_rss_bytes <= 0:
        raise BridgeWorkerIsolationError(
            "native-memory bridge child returned no kernel RSS high-water evidence",
            admission=admission,
            telemetry=telemetry(admission_seconds),
            attempt_id=attempt_id,
        )
    rss_sample_count += 1
    peak_rss_bytes = child_peak_rss_bytes if peak_rss_bytes is None else max(peak_rss_bytes, child_peak_rss_bytes)
    if status == "memory" or child_peak_rss_bytes > max_rss_bytes:
        raise BridgeWorkerMemoryExceeded(
            f"native-memory bridge exceeded {max_rss_bytes // (1024 * 1024)} MiB kernel RSS high-water limit",
            admission=admission,
            telemetry=telemetry(admission_seconds),
            attempt_id=attempt_id,
        )
    if status != "ok":
        raise BridgeWorkerIsolationError(
            f"native-memory bridge child failed: {payload}",
            admission=admission,
            telemetry=telemetry(admission_seconds),
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
    final_telemetry = telemetry(admission_seconds)
    try:
        with _deadline_alarm(deadline, "receipt and telemetry finalization"):
            if isinstance(bridge, NativeMemoryBridge) and payload.status != "busy":
                bridge.record_supervisor_telemetry(attempt_id, final_telemetry)
            elif payload.status != "busy":
                bridge.record_supervisor_telemetry(final_telemetry)
    except _DeadlineExpired as exc:
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit during {exc}",
            admission=admission,
            telemetry=final_telemetry,
            attempt_id=attempt_id,
        ) from None
    except Exception as exc:
        raise BridgeWorkerIsolationError(
            f"native-memory bridge supervisor finalization failed: {exc}",
            admission=admission,
            telemetry=final_telemetry,
            attempt_id=attempt_id,
        ) from exc
    if time.monotonic() > deadline:
        raise BridgeWorkerTimeout(
            f"native-memory bridge exceeded {timeout_seconds}s outer limit after receipt finalization",
            admission=admission,
            telemetry=final_telemetry,
            attempt_id=attempt_id,
        )
    return payload
