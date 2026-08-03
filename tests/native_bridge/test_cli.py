from __future__ import annotations

import json

from click.testing import CliRunner

import observational_memory.cli as cli_module
from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.native_bridge.admission import AdmissionResult
from observational_memory.native_bridge.bridge import BridgeResult
from observational_memory.native_bridge.worker import BridgeWorkerTimeout


def test_cli_reports_immutable_admission_evidence_from_temp_config(tmp_path, monkeypatch):
    """P3 contract: immediate JSON preserves the exact frozen admission snapshot."""
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    admission = AdmissionResult(True, "normal", 123, 456, "bounded evidence")
    monkeypatch.setattr(config, "load_env_file", lambda: (_ for _ in ()).throw(AssertionError("env file read")))
    monkeypatch.setattr(cli_module, "Config", lambda: config)
    monkeypatch.setattr(
        "observational_memory.native_bridge.worker.run_bounded_bridge",
        lambda bridge: BridgeResult(
            "success",
            0,
            "committed",
            admission,
            "generation",
            "digest",
            attempt_id="a" * 32,
        ),
    )

    result = CliRunner().invoke(cli, ["bridge-native-memory", "--claude-project", "project-a", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "attempt_id": "a" * 32,
        "desired_state_digest": "digest",
        "exit_code": 0,
        "generation_id": "generation",
        "message": "committed",
        "resource_profile": "strict-default",
        "resource_limits": None,
        "output_root": None,
        "durable_receipt_written": True,
        "status": "success",
        "admission": {
            "admitted": True,
            "pressure": "normal",
            "swap_used_bytes": 123,
            "swap_total_bytes": 456,
            "reason": "bounded evidence",
            "resource_profile": "strict-default",
        },
    }


def test_cli_propagates_nonzero_busy_exit(tmp_path, monkeypatch):
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    monkeypatch.setattr(cli_module, "Config", lambda: config)
    monkeypatch.setattr(
        "observational_memory.native_bridge.worker.run_bounded_bridge",
        lambda bridge: BridgeResult(
            "busy",
            75,
            "lock busy",
            AdmissionResult(False, "normal", 1, 100, "store busy"),
        ),
    )

    result = CliRunner().invoke(cli, ["bridge-native-memory", "--claude-project", "project-a", "--json"])

    assert result.exit_code == 75
    assert json.loads(result.output)["status"] == "busy"


def test_cli_records_outer_failure_with_original_admission_and_telemetry(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    config = Config(
        memory_dir=memory,
        env_file=tmp_path / "config" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    admission = AdmissionResult(True, "normal", 1, 100, "admitted")
    monkeypatch.setattr(cli_module, "Config", lambda: config)
    monkeypatch.setattr(
        "observational_memory.native_bridge.worker.run_bounded_bridge",
        lambda _bridge: (_ for _ in ()).throw(
            BridgeWorkerTimeout(
                "bounded timeout",
                admission=admission,
                telemetry={"peak_tree_rss_bytes": 1234, "rss_sample_count": 1},
            )
        ),
    )

    result = CliRunner().invoke(cli, ["bridge-native-memory", "--claude-project", "project-a", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "failed"
    status = json.loads((memory / ".native-memory-bridge" / "status.json").read_text())
    assert status["admission"]["pressure"] == "normal"
    assert status["telemetry"]["peak_tree_rss_bytes"] == 1234


def test_cli_does_not_expose_trial_or_codex_allowlist_overrides(tmp_path, monkeypatch):
    """Invariant: product CLI has no temporary trial or arbitrary Codex source route."""
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    monkeypatch.setattr(cli_module, "Config", lambda: config)

    trial = CliRunner().invoke(cli, ["bridge-native-memory", "--resource-profile", "temporary-light-canary"])
    codex_override = CliRunner().invoke(cli, ["bridge-native-memory", "--codex-file", "other.md"])

    assert trial.exit_code == 2
    assert "No such option '--resource-profile'" in trial.output
    assert codex_override.exit_code == 2
    assert "No such option '--codex-file'" in codex_override.output


def test_cli_selects_fixed_product_profile_and_codex_allowlist(tmp_path, monkeypatch):
    """Invariant: the public one-shot route cannot widen fixed product boundaries."""
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude",
        search_backend="bm25",
    )
    selected = {}

    def run(bridge):
        selected["bridge"] = bridge
        return BridgeResult(
            "success",
            0,
            "committed",
            AdmissionResult(
                True,
                "normal",
                100,
                1000,
                "admitted",
                bridge.resource_profile.name,
            ),
            resource_profile=bridge.resource_profile.name,
            resource_limits=bridge.resource_limits(),
            output_root=str(bridge.state_dir),
        )

    monkeypatch.setattr(cli_module, "Config", lambda: config)
    monkeypatch.setattr("observational_memory.native_bridge.worker.run_bounded_bridge", run)
    result = CliRunner().invoke(
        cli,
        [
            "bridge-native-memory",
            "--claude-project",
            "one-project",
            "--json",
        ],
    )

    assert result.exit_code == 0
    bridge = selected["bridge"]
    assert bridge.resource_profile.name == "strict-default"
    assert bridge.policy.codex_allowlist == ("MEMORY.md", "memory_summary.md")
    assert bridge.policy.claude_projects == ("one-project",)
    assert bridge.state_dir == config.native_bridge_data_dir
