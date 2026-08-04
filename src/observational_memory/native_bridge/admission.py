"""Fail-closed host admission checks for the bounded bridge."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal

from .profiles import STRICT_DEFAULT_PROFILE, BridgeResourceProfile

_SWAP_OUTPUT = re.compile(
    r"total = ([0-9]+(?:\.[0-9]+)?)([MG])  "
    r"used = ([0-9]+(?:\.[0-9]+)?)([MG])  "
    r"free = ([0-9]+(?:\.[0-9]+)?)([MG])  "
    r"\(encrypted\)\n"
)


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    pressure: str
    swap_used_bytes: int
    swap_total_bytes: int
    reason: str
    resource_profile: str = STRICT_DEFAULT_PROFILE.name

    @property
    def swap_fraction(self) -> float:
        if self.swap_total_bytes == 0 and self.swap_used_bytes == 0:
            return 0.0
        if self.swap_total_bytes <= 0:
            return 1.0
        return self.swap_used_bytes / self.swap_total_bytes


def evaluate_admission(
    *,
    pressure: str,
    swap_used_bytes: int,
    swap_total_bytes: int,
    profile: BridgeResourceProfile = STRICT_DEFAULT_PROFILE,
) -> AdmissionResult:
    normalized = pressure.strip().lower()
    if normalized not in {"normal", "warning", "critical"}:
        return AdmissionResult(
            False,
            normalized or "probe-error",
            swap_used_bytes,
            swap_total_bytes,
            "pressure probe error",
            profile.name,
        )
    if normalized not in profile.allowed_pressure:
        return AdmissionResult(
            False,
            normalized,
            swap_used_bytes,
            swap_total_bytes,
            f"memory pressure is {normalized}",
            profile.name,
        )
    if (
        type(swap_used_bytes) is not int
        or type(swap_total_bytes) is not int
        or swap_total_bytes < 0
        or swap_used_bytes < 0
        or swap_used_bytes > swap_total_bytes
    ):
        return AdmissionResult(
            False,
            normalized,
            swap_used_bytes,
            swap_total_bytes,
            "swap probe error",
            profile.name,
        )
    fraction = 0.0 if swap_total_bytes == 0 else swap_used_bytes / swap_total_bytes
    if fraction > 0.80:
        return AdmissionResult(
            False,
            normalized,
            swap_used_bytes,
            swap_total_bytes,
            "swap use is above 80%",
            profile.name,
        )
    return AdmissionResult(
        True,
        normalized,
        swap_used_bytes,
        swap_total_bytes,
        "admitted",
        profile.name,
    )


def _probe_pressure() -> str:
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        raise RuntimeError("memory-pressure sysctl failed")
    if result.stderr:
        raise RuntimeError("memory-pressure sysctl returned diagnostic output")
    value = result.stdout.strip()
    mapping = {"1": "normal", "2": "warning", "4": "critical"}
    if value not in mapping or result.stdout.count("\n") > 1:
        raise RuntimeError("unrecognized memory-pressure level")
    return mapping[value]


def _probe_swap() -> tuple[int, int]:
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3)
    if result.returncode != 0:
        raise RuntimeError("swap probe failed")
    if result.stderr:
        raise RuntimeError("swap probe returned diagnostic output")
    match = _SWAP_OUTPUT.fullmatch(result.stdout)
    if not match:
        raise RuntimeError("unrecognized swap output")

    def as_byte_quantity(value: str, unit: str) -> tuple[Decimal, Decimal]:
        multiplier = 1024**3 if unit == "G" else 1024**2
        places = len(value.partition(".")[2])
        quantum = Decimal(1).scaleb(-places) * multiplier
        return Decimal(value) * multiplier, quantum

    total_value, total_quantum = as_byte_quantity(match.group(1), match.group(2))
    used_value, used_quantum = as_byte_quantity(match.group(3), match.group(4))
    free_value, free_quantum = as_byte_quantity(match.group(5), match.group(6))
    if used_value > total_value or free_value > total_value:
        raise RuntimeError("inconsistent swap output")
    if total_value == 0 and (used_value != 0 or free_value != 0):
        raise RuntimeError("inconsistent zero-swap output")
    rounding_tolerance = total_quantum + used_quantum + free_quantum
    if abs(total_value - used_value - free_value) > rounding_tolerance:
        raise RuntimeError("inconsistent swap output")
    total = int(total_value)
    used = int(used_value)
    if total_value > 0 and total == 0:
        raise RuntimeError("swap output is below byte precision")
    return used, total


def probe_admission(
    profile: BridgeResourceProfile = STRICT_DEFAULT_PROFILE,
) -> AdmissionResult:
    """Probe pressure and swap; any error rejects admission."""
    try:
        pressure = _probe_pressure()
        used, total = _probe_swap()
    except Exception as exc:
        return AdmissionResult(
            False,
            "probe-error",
            0,
            0,
            f"admission probe failed: {exc}",
            profile.name,
        )
    return evaluate_admission(
        pressure=pressure,
        swap_used_bytes=used,
        swap_total_bytes=total,
        profile=profile,
    )
