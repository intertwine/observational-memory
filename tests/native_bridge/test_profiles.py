"""Deterministic admission, budget, and isolation contracts for bridge profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observational_memory.config import Config
from observational_memory.native_bridge.admission import AdmissionResult, evaluate_admission
from observational_memory.native_bridge.bridge import BridgePolicy, NativeMemoryBridge
from observational_memory.native_bridge.profiles import (
    STRICT_DEFAULT_PROFILE,
    TEMPORARY_LIGHT_CANARY_PROFILE,
)
from observational_memory.native_bridge.secure_fs import SecureAccessError
from observational_memory.native_bridge.worker import run_bounded_bridge


def _light_fixture(tmp_path):
    memory = tmp_path / "isolated-om"
    memory.mkdir(mode=0o700)
    codex_home = tmp_path / "codex"
    codex_memories = codex_home / "memories"
    codex_memories.mkdir(parents=True)
    (codex_memories / "memory_summary.md").write_text("# Summary\n\nCodex light fact.")
    claude_root = tmp_path / "claude"
    claude_memory = claude_root / "project-a" / "memory"
    claude_memory.mkdir(parents=True)
    (claude_memory / "MEMORY.md").write_text("# Claude\n\nClaude light fact.")
    output = tmp_path / "dedicated-light-output"
    output.mkdir(mode=0o700)
    config = Config(
        memory_dir=memory,
        env_file=tmp_path / "config" / "env",
        codex_home=codex_home,
        claude_projects_dir=claude_root,
        search_backend="bm25",
    )
    profile = TEMPORARY_LIGHT_CANARY_PROFILE
    policy = BridgePolicy(
        claude_projects=("project-a",),
        codex_allowlist=profile.codex_allowlist,
        max_file_bytes=profile.max_file_input_bytes,
        max_total_bytes=profile.max_total_input_bytes,
    )
    return config, policy, output, codex_memories, claude_memory


@pytest.mark.parametrize(
    ("profile", "pressure", "used", "total", "admitted"),
    [
        (STRICT_DEFAULT_PROFILE, "normal", 80, 100, True),
        (STRICT_DEFAULT_PROFILE, "warning", 1, 100, False),
        (STRICT_DEFAULT_PROFILE, "critical", 1, 100, False),
        (STRICT_DEFAULT_PROFILE, "unknown", 1, 100, False),
        (STRICT_DEFAULT_PROFILE, "normal", 81, 100, False),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "normal", 80, 100, True),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "warning", 80, 100, True),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "critical", 1, 100, False),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "unknown", 1, 100, False),
        (TEMPORARY_LIGHT_CANARY_PROFILE, "warning", 81, 100, False),
    ],
)
def test_profile_pressure_and_swap_matrix(profile, pressure, used, total, admitted):
    """Invariant: only profile-approved pressure and swap at or below 80% admit."""
    result = evaluate_admission(
        pressure=pressure,
        swap_used_bytes=used,
        swap_total_bytes=total,
        profile=profile,
    )
    assert result.admitted is admitted
    assert result.resource_profile == profile.name


@pytest.mark.parametrize(("used", "total"), [(-1, 100), (1, 0), (1, -1), (101, 100)])
@pytest.mark.parametrize("profile", [STRICT_DEFAULT_PROFILE, TEMPORARY_LIGHT_CANARY_PROFILE])
def test_both_profiles_fail_closed_on_malformed_swap_evidence(profile, used, total):
    """Invariant: missing or malformed swap evidence rejects every profile."""
    result = evaluate_admission(
        pressure="normal",
        swap_used_bytes=used,
        swap_total_bytes=total,
        profile=profile,
    )
    assert result.admitted is False
    assert "swap probe error" in result.reason


def test_light_profile_has_exact_temporary_resource_limits():
    """Invariant: the manual light route cannot exceed 15s, 128MiB, 4MiB, or 1MiB."""
    profile = TEMPORARY_LIGHT_CANARY_PROFILE
    assert profile.timeout_seconds == 15
    assert profile.max_rss_bytes == 128 * 1024 * 1024
    assert profile.max_total_input_bytes == 4 * 1024 * 1024
    assert profile.max_file_input_bytes == 1 * 1024 * 1024
    assert profile.codex_allowlist == ("memory_summary.md",)
    assert profile.max_claude_projects == 1
    assert profile.manual_only is True


def test_strict_profile_enforces_production_15s_and_128mib_hard_maxima():
    """Invariant: every production run has the reviewed worker ceilings."""
    assert STRICT_DEFAULT_PROFILE.timeout_seconds == 15
    assert STRICT_DEFAULT_PROFILE.max_rss_bytes == 128 * 1024 * 1024


def test_worker_rejects_limits_above_selected_profile_before_admission(tmp_path):
    """Invariant: caller arguments can reduce but never enlarge profile limits."""
    config, policy, output, _codex, _claude = _light_fixture(tmp_path)
    admission_called = False

    def probe():
        nonlocal admission_called
        admission_called = True
        return AdmissionResult(
            True,
            "warning",
            1,
            100,
            "admitted",
            TEMPORARY_LIGHT_CANARY_PROFILE.name,
        )

    bridge = NativeMemoryBridge(
        config,
        policy,
        resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
        output_root=output,
        admission_probe=probe,
    )
    with pytest.raises(ValueError, match="at most 15s"):
        run_bounded_bridge(bridge, timeout_seconds=16)
    with pytest.raises(ValueError, match="at most 134217728"):
        run_bounded_bridge(bridge, max_rss_bytes=128 * 1024 * 1024 + 1)
    assert admission_called is False


def test_light_profile_requires_manual_invocation_exact_scope_and_temporary_root(tmp_path):
    """Invariant: scheduler use, broad Codex scope, or missing Claude opt-in is rejected."""
    config, policy, output, _codex, _claude = _light_fixture(tmp_path)
    with pytest.raises(SecureAccessError, match="manual-only"):
        NativeMemoryBridge(
            config,
            policy,
            resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
            output_root=output,
            invocation="scheduler",
        )
    with pytest.raises(SecureAccessError, match="memory_summary"):
        NativeMemoryBridge(
            config,
            BridgePolicy(
                claude_projects=("project-a",),
                codex_allowlist=("MEMORY.md",),
                max_file_bytes=1024,
                max_total_bytes=4096,
            ),
            resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
            output_root=output,
        )
    with pytest.raises(SecureAccessError, match="exactly one"):
        NativeMemoryBridge(
            config,
            BridgePolicy(
                codex_allowlist=("memory_summary.md",),
                max_file_bytes=1024,
                max_total_bytes=4096,
            ),
            resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
            output_root=output,
        )


def test_light_profile_rejects_live_or_non_temporary_output_roots(tmp_path):
    """Invariant: light output cannot target OM, native memory, search, or non-temp roots."""
    config, policy, _output, _codex, _claude = _light_fixture(tmp_path)
    with pytest.raises(SecureAccessError, match="disjoint"):
        NativeMemoryBridge(
            config,
            policy,
            resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
            output_root=config.memory_dir,
        )
    with pytest.raises(SecureAccessError, match="temporary directory"):
        NativeMemoryBridge(
            config,
            policy,
            resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
            output_root=Path.home() / "non-temporary-om-light-output",
        )


@pytest.mark.parametrize("pressure", ["normal", "warning"])
def test_light_profile_run_records_exact_limits_and_writes_only_dedicated_root(tmp_path, pressure):
    """Invariant: admitted light runs use the dedicated root and exact profile receipt."""
    config, policy, output, _codex, _claude = _light_fixture(tmp_path)
    admission = AdmissionResult(
        True,
        pressure,
        80,
        100,
        "admitted",
        TEMPORARY_LIGHT_CANARY_PROFILE.name,
    )
    bridge = NativeMemoryBridge(
        config,
        policy,
        resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
        output_root=output,
        admission_probe=lambda: admission,
    )
    result = bridge.run()
    assert result.status == "success"
    assert result.resource_profile == TEMPORARY_LIGHT_CANARY_PROFILE.name
    assert result.output_root == str(output)
    assert not (config.memory_dir / ".native-memory-bridge").exists()
    marker = json.loads((output / "light-canary-profile.json").read_text())
    receipt = json.loads((output / "receipts" / f"{result.attempt_id}.json").read_text())
    assert marker["resource_limits"] == result.resource_limits
    assert receipt["resource_limits"] == result.resource_limits
    assert receipt["admission"]["pressure"] == pressure
    assert receipt["output_root"] == str(output)


def test_light_profile_enforces_one_mib_file_budget(tmp_path):
    """Invariant: an oversized selected file fails before materialization or pointer publication."""
    config, policy, output, codex, _claude = _light_fixture(tmp_path)
    (codex / "memory_summary.md").write_bytes(b"x" * (1024 * 1024 + 1))
    admission = AdmissionResult(
        True,
        "normal",
        1,
        100,
        "admitted",
        TEMPORARY_LIGHT_CANARY_PROFILE.name,
    )
    result = NativeMemoryBridge(
        config,
        policy,
        resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
        output_root=output,
        admission_probe=lambda: admission,
    ).run()
    assert result.status == "failed"
    assert "1048576 byte limit" in result.message
    assert not (output / "current-generation.json").exists()


def test_light_profile_enforces_four_mib_total_budget(tmp_path):
    """Invariant: selected files cannot exceed the light profile aggregate ceiling."""
    config, policy, output, codex, claude = _light_fixture(tmp_path)
    (codex / "memory_summary.md").write_bytes(b"c" * (1024 * 1024))
    for index in range(4):
        (claude / f"part-{index}.md").write_bytes(bytes([65 + index]) * (1024 * 1024))
    admission = AdmissionResult(
        True,
        "warning",
        1,
        100,
        "admitted",
        TEMPORARY_LIGHT_CANARY_PROFILE.name,
    )
    result = NativeMemoryBridge(
        config,
        policy,
        resource_profile=TEMPORARY_LIGHT_CANARY_PROFILE,
        output_root=output,
        admission_probe=lambda: admission,
    ).run()
    assert result.status == "failed"
    assert "4194304 byte limit" in result.message
    assert not (output / "current-generation.json").exists()
