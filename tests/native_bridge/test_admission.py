from __future__ import annotations

import subprocess

import pytest

from observational_memory.native_bridge import admission
from observational_memory.native_bridge.admission import evaluate_admission
from observational_memory.native_bridge.profiles import (
    STRICT_DEFAULT_PROFILE,
    TEMPORARY_LIGHT_CANARY_PROFILE,
)


def test_exactly_eighty_percent_swap_is_admitted():
    result = evaluate_admission(pressure="normal", swap_used_bytes=8000, swap_total_bytes=10000)
    assert result.admitted is True
    assert result.swap_fraction == 0.8


def test_swap_above_eighty_percent_is_rejected():
    result = evaluate_admission(pressure="normal", swap_used_bytes=8001, swap_total_bytes=10000)
    assert result.admitted is False
    assert "above 80%" in result.reason


@pytest.mark.parametrize(
    ("profile", "pressure"),
    [
        (STRICT_DEFAULT_PROFILE, "normal"),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "normal"),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "warning"),
    ],
)
def test_canonical_zero_swap_is_admitted_under_allowed_pressure(profile, pressure):
    """Invariant: canonical 0/0 swap means zero use, subject to the profile pressure gate."""
    result = evaluate_admission(
        pressure=pressure,
        swap_used_bytes=0,
        swap_total_bytes=0,
        profile=profile,
    )

    assert result.admitted is True
    assert result.swap_fraction == 0.0


@pytest.mark.parametrize("pressure", ["warning", "critical"])
def test_warning_and_critical_pressure_are_rejected(pressure):
    result = evaluate_admission(pressure=pressure, swap_used_bytes=1, swap_total_bytes=100)
    assert result.admitted is False
    assert pressure in result.reason


@pytest.mark.parametrize(
    ("pressure", "used", "total"),
    [
        ("unknown", 1, 100),
        ("normal", -1, 100),
        ("normal", 1, 0),
        ("normal", 101, 100),
        ("normal", 0, -1),
    ],
)
def test_invalid_admission_values_fail_closed(pressure, used, total):
    assert evaluate_admission(pressure=pressure, swap_used_bytes=used, swap_total_bytes=total).admitted is False


@pytest.mark.parametrize(
    ("used", "total"),
    [
        (False, False),
        (False, 0),
        (0, False),
        (0.0, 0.0),
        (0.0, 0),
        (0, 0.0),
    ],
)
def test_non_integer_zero_swap_values_fail_closed(used, total):
    """Invariant: only exact integers can enter the canonical zero-swap exception."""
    result = evaluate_admission(
        pressure="normal",
        swap_used_bytes=used,
        swap_total_bytes=total,
    )

    assert result.admitted is False
    assert result.reason == "swap probe error"


def test_probe_error_fails_closed(monkeypatch):
    monkeypatch.setattr(admission, "_probe_pressure", lambda: (_ for _ in ()).throw(RuntimeError("probe failed")))

    result = admission.probe_admission()

    assert result.admitted is False
    assert result.pressure == "probe-error"
    assert "probe failed" in result.reason


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("1\n", "normal"),
        ("2\n", "warning"),
        ("4\n", "critical"),
    ],
)
def test_pressure_probe_accepts_only_authoritative_current_levels(monkeypatch, level, expected):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=level, stderr=""),
    )

    assert admission._probe_pressure() == expected


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode"),
    [
        ("System-wide memory free percentage: 71%\n", "", 0),
        ("normal\n", "", 0),
        ("0\n", "", 0),
        ("3\n", "", 0),
        ("1\nextra\n", "", 0),
        ("1\n", "diagnostic\n", 0),
        ("1\n", "", 1),
    ],
)
def test_pressure_probe_rejects_non_authoritative_or_ambiguous_output(
    monkeypatch,
    stdout,
    stderr,
    returncode,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr),
    )

    with pytest.raises(RuntimeError):
        admission._probe_pressure()


@pytest.mark.parametrize(
    "profile",
    [STRICT_DEFAULT_PROFILE, TEMPORARY_LIGHT_CANARY_PROFILE],
    ids=lambda profile: profile.name,
)
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (
            "diagnostic prefix\ntotal = 100.00M used = 1.00M free = 99.00M\n",
            "",
        ),
        (
            "total = 100.00M used = 1.00M free = 99.00M\n",
            "diagnostic output\n",
        ),
        (
            "total = 100.00M used = 1.00M free = 99.00M\ntotal = 100.00M used = 99.00M free = 1.00M\n",
            "",
        ),
    ],
    ids=["diagnostic-prefix", "diagnostic-stderr", "multiple-records"],
)
def test_malformed_ambiguous_or_diagnostic_swap_evidence_fails_closed(
    monkeypatch,
    profile,
    stdout,
    stderr,
):
    """Invariant: invalid swap evidence cannot admit either resource profile."""
    monkeypatch.setattr(admission, "_probe_pressure", lambda: "normal")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr),
    )

    result = admission.probe_admission(profile)

    assert result.admitted is False
    assert result.pressure == "probe-error"
    assert result.resource_profile == profile.name


def test_probe_accepts_canonical_zero_swap_output(monkeypatch):
    """Invariant: the authoritative macOS 0/0/0 record is usable zero-pressure evidence."""
    monkeypatch.setattr(admission, "_probe_pressure", lambda: "warning")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)\n",
            stderr="",
        ),
    )

    result = admission.probe_admission(TEMPORARY_LIGHT_CANARY_PROFILE)

    assert result.admitted is True
    assert result.swap_used_bytes == 0
    assert result.swap_total_bytes == 0
    assert result.swap_fraction == 0.0


@pytest.mark.parametrize(
    "stdout",
    [
        "total = 0.00M  used = 1.00M  free = 0.00M  (encrypted)\n",
        "total = 0.00M  used = 0.00M  free = 1.00M  (encrypted)\n",
        "total = 1.00M  used = 2.00M  free = 0.00M  (encrypted)\n",
        "total = 1.00M  used = 0.00M  free = 2.00M  (encrypted)\n",
        "total = 100.00M  used = 80.00M  free = 80.00M  (encrypted)\n",
        "total = -1.00M  used = 0.00M  free = 0.00M  (encrypted)\n",
    ],
)
def test_probe_rejects_contradictory_or_negative_swap_output(monkeypatch, stdout):
    """Invariant: zero-total exceptions never admit contradictory or negative evidence."""
    monkeypatch.setattr(admission, "_probe_pressure", lambda: "normal")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
    )

    result = admission.probe_admission(TEMPORARY_LIGHT_CANARY_PROFILE)

    assert result.admitted is False
    assert result.pressure == "probe-error"
