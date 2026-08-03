from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

import observational_memory.cli as cli_module
import observational_memory.native_bridge.lifecycle as lifecycle
from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.native_bridge import BridgePolicy, NativeMemoryBridge
from observational_memory.native_bridge.admission import AdmissionResult
from observational_memory.native_bridge.lifecycle import (
    LaunchdState,
    NativeBridgeLifecycleError,
    NativeBridgeSettings,
    load_settings,
    snapshot_owned_file,
    write_settings,
)


def _config(monkeypatch, tmp_path: Path, *, search_backend: str = "bm25") -> Config:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Config(
        memory_dir=tmp_path / "data" / "observational-memory",
        env_file=tmp_path / "config" / "observational-memory" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude" / "projects",
        search_backend=search_backend,
    )


def _patch_cli_config(monkeypatch, config: Config, *, forbid_env_load: bool = False) -> None:
    monkeypatch.setattr(cli_module, "Config", lambda: config)
    if forbid_env_load:
        monkeypatch.setattr(
            config,
            "load_env_file",
            lambda: (_ for _ in ()).throw(AssertionError("provider env must not load")),
        )
    else:
        monkeypatch.setattr(config, "load_env_file", lambda: None)


def test_install_native_bridge_bypasses_provider_and_legacy_installers(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(cli_module, "_find_om_path", lambda: "/opt/om/bin/om")
    monkeypatch.setattr(
        cli_module,
        "_configure_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider setup must not run")),
    )
    monkeypatch.setattr(
        cli_module,
        "_install_claude_hooks",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy Claude install must not run")),
    )
    monkeypatch.setattr(
        cli_module,
        "_install_codex",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy Codex install must not run")),
    )
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "quiesce_legacy_launchd", lambda _config: calls.append("quiesce") or {})
    monkeypatch.setattr(
        lifecycle,
        "install_bridge_launchd",
        lambda _config, om_path: calls.append(f"install:{om_path}"),
    )

    result = CliRunner().invoke(
        cli,
        ["install", "--native-bridge", "--claude-project", "-Users-example-project"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["quiesce", "install:/opt/om/bin/om"]
    assert load_settings(config) == NativeBridgeSettings(("-Users-example-project",))
    assert "om search --native-bridge" in result.output
    assert not config.env_file.exists()


def test_failed_activation_restores_config_hooks_and_legacy_state(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(cli_module, "_find_om_path", lambda: "/opt/om/bin/om")
    write_settings(config, NativeBridgeSettings(("old-project",)))

    config.claude_settings_path.parent.mkdir(parents=True)
    original_claude = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "/opt/om/bin/om context"}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "/opt/om/bin/om claude-checkpoint"}]}],
        },
        "preserve": True,
    }
    config.claude_settings_path.write_text(json.dumps(original_claude, indent=2) + "\n")
    before_config = config.native_bridge_config_path.read_bytes()
    before_claude = config.claude_settings_path.read_bytes()

    legacy_state = {"legacy": LaunchdState("legacy", installed=True, loaded=True, override="enabled")}
    restored: list[dict[str, LaunchdState]] = []
    monkeypatch.setattr(lifecycle, "quiesce_legacy_launchd", lambda _config: legacy_state)
    monkeypatch.setattr(
        lifecycle,
        "install_bridge_launchd",
        lambda *_args: (_ for _ in ()).throw(NativeBridgeLifecycleError("injected activation failure")),
    )
    monkeypatch.setattr(
        lifecycle,
        "restore_launchd_states",
        lambda _config, states: restored.append(states),
    )

    result = CliRunner().invoke(cli, ["install", "--native-bridge", "--claude-project", "new-project"])

    assert result.exit_code == 1
    assert "installation failed" in result.output
    assert config.native_bridge_config_path.read_bytes() == before_config
    assert config.claude_settings_path.read_bytes() == before_claude
    assert restored == [legacy_state]


def test_install_fails_closed_and_preserves_concurrent_hook_edit(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(cli_module, "_find_om_path", lambda: "/opt/om/bin/om")
    config.claude_settings_path.parent.mkdir(parents=True)
    config.claude_settings_path.write_text(
        json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"command": "/opt/om/bin/om claude-checkpoint"}]}]}})
    )
    concurrent = b'{"hooks":{},"unrelated_concurrent_edit":true}\n'
    original_write = lifecycle.atomic_write_owned_file

    def race(path, data, *, mode=0o600, expected_snapshot=None):
        if path == config.claude_settings_path:
            path.write_bytes(concurrent)
        return original_write(
            path,
            data,
            mode=mode,
            expected_snapshot=expected_snapshot,
        )

    monkeypatch.setattr(lifecycle, "atomic_write_owned_file", race)
    monkeypatch.setattr(
        lifecycle,
        "quiesce_legacy_launchd",
        lambda _config: (_ for _ in ()).throw(AssertionError("launchd must not run after a concurrent edit")),
    )

    result = CliRunner().invoke(cli, ["install", "--native-bridge", "--claude-project", "project-a"])

    assert result.exit_code == 1
    assert "concurrently changed" in result.output
    assert config.claude_settings_path.read_bytes() == concurrent
    assert not config.native_bridge_config_path.exists()


def test_ordinary_runtime_paths_explicitly_load_provider_env(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(config, "load_env_file", lambda: calls.append("load"))
    monkeypatch.setattr(cli_module, "Config", lambda: config)
    ctx = SimpleNamespace(obj={"config": config})

    selected = cli_module._load_runtime_config(ctx)

    assert selected is config
    assert ctx.obj["config"] is config
    assert calls == ["load"]


def test_native_bridge_purge_uninstalls_and_verifies_service_first(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "uninstall_bridge_launchd", lambda _config: calls.append("uninstall"))

    def purge(_config):
        assert calls == ["uninstall"]
        calls.append("purge")
        return (config.native_bridge_data_dir,)

    monkeypatch.setattr(lifecycle, "purge_bridge_state", purge)

    result = CliRunner().invoke(cli, ["uninstall", "--native-bridge", "--purge"])

    assert result.exit_code == 0, result.output
    assert calls == ["uninstall", "purge"]
    assert "absence verified" in result.output
    assert "derived generations" in result.output


def test_hook_quiescence_removes_only_om_writer_groups(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    config.claude_settings_path.parent.mkdir(parents=True)
    config.codex_hooks_path.parent.mkdir(parents=True)
    cowork_path = cli_module._cowork_plugin_dir(config) / "hooks" / "hooks.json"
    cowork_path.parent.mkdir(parents=True)

    unrelated_hook = {"type": "command", "command": "/usr/bin/true", "custom": "keep-hook"}
    unrelated = {"hooks": [unrelated_hook]}
    claude_mixed = {
        "matcher": "keep-claude-matcher",
        "custom": {"preserve": True},
        "hooks": [
            unrelated_hook,
            {"type": "command", "command": "/opt/om/bin/om claude-checkpoint"},
        ],
    }
    codex_mixed = {
        "matcher": "keep-codex-matcher",
        "custom": {"preserve": True},
        "hooks": [
            unrelated_hook,
            {
                "type": "command",
                "command": "/opt/om/bin/om codex-checkpoint",
                "statusMessage": "Checkpointing observational memory...",
            },
        ],
    }
    cowork_mixed = {
        "matcher": "keep-cowork-matcher",
        "custom": {"preserve": True},
        "hooks": [
            unrelated_hook,
            {
                "type": "command",
                "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session-end.sh",
            },
        ],
    }
    config.claude_settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [unrelated],
                    "SessionEnd": [claude_mixed, unrelated],
                }
            }
        )
    )
    config.codex_hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [unrelated],
                    "Stop": [codex_mixed, unrelated],
                }
            }
        )
    )
    cowork_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [unrelated],
                    "SessionEnd": [cowork_mixed, unrelated],
                }
            }
        )
    )
    snapshots = {
        path: snapshot_owned_file(path, max_bytes=1024 * 1024)
        for path in (config.claude_settings_path, config.codex_hooks_path, cowork_path)
    }

    stop_hook, stop_error = cli_module._find_codex_stop_hook(config)
    assert stop_error is None
    assert stop_hook is not None
    assert stop_hook["command"].endswith(" codex-checkpoint")

    updates = cli_module._render_native_bridge_hook_updates(config, snapshots)

    claude = json.loads(updates[config.claude_settings_path])
    codex = json.loads(updates[config.codex_hooks_path])
    cowork = json.loads(updates[cowork_path])
    assert claude["hooks"]["SessionStart"] == [unrelated]
    assert claude["hooks"]["SessionEnd"] == [
        {"matcher": "keep-claude-matcher", "custom": {"preserve": True}, "hooks": [unrelated_hook]},
        unrelated,
    ]
    assert codex["hooks"]["SessionStart"] == [unrelated]
    assert codex["hooks"]["Stop"] == [
        {"matcher": "keep-codex-matcher", "custom": {"preserve": True}, "hooks": [unrelated_hook]},
        unrelated,
    ]
    assert cowork["hooks"]["SessionStart"] == [unrelated]
    assert cowork["hooks"]["SessionEnd"] == [
        {"matcher": "keep-cowork-matcher", "custom": {"preserve": True}, "hooks": [unrelated_hook]},
        unrelated,
    ]


def test_sources_command_is_counts_only_and_does_not_persist_selection(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    memory = config.claude_projects_dir / "-Users-example-project" / "memory"
    memory.mkdir(parents=True)
    (memory / "topic.md").write_text("never print this private sentence")
    codex_memories = config.codex_home / "memories"
    codex_memories.mkdir(parents=True)
    (codex_memories / "MEMORY.md").write_text("never print this codex sentence")

    result = CliRunner().invoke(cli, ["native-bridge", "sources", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claude_projects"] == [{"eligible_markdown_files": 1, "project": "-Users-example-project"}]
    assert payload["codex_allowlist"] == ["MEMORY.md", "memory_summary.md"]
    assert payload["codex_sources"] == [
        {"filename": "MEMORY.md", "present": True},
        {"filename": "memory_summary.md", "present": False},
    ]
    assert payload["auto_enrolled"] is False
    assert "never print" not in result.output
    assert not config.native_bridge_config_path.exists()


def test_public_search_reads_verified_isolated_generation_with_qmd_config(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path, search_backend="qmd-hybrid")
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    codex_memories = config.codex_home / "memories"
    claude_memory = config.claude_projects_dir / "project-a" / "memory"
    codex_memories.mkdir(parents=True)
    claude_memory.mkdir(parents=True)
    (codex_memories / "memory_summary.md").write_text("codex shared quasarneedle fact\n")
    (claude_memory / "topic.md").write_text("claude shared auroraneedle decision\n")
    bridge = NativeMemoryBridge(
        config,
        BridgePolicy(claude_projects=("project-a",)),
    )
    published = bridge.run_with_admission(AdmissionResult(True, "normal", 0, 0, "admitted"))
    assert published.status == "success", published.message

    result = CliRunner().invoke(cli, ["search", "auroraneedle", "--native-bridge", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["source"] == "native_memory"
    assert "auroraneedle" in payload[0]["content"]
    assert not config.search_index_dir.exists()


def test_public_search_fails_clearly_before_first_bridge_generation(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path, search_backend="none")
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)

    result = CliRunner().invoke(cli, ["search", "anything", "--native-bridge"])

    assert result.exit_code == 1
    assert "no native bridge generation is available" in result.output
    assert "om install --native-bridge" in result.output


def test_bridge_mode_doctor_is_provider_free_and_never_recommends_legacy_install(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _patch_cli_config(monkeypatch, config, forbid_env_load=True)
    write_settings(config, NativeBridgeSettings(("project-a",)))
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")

    def inspect(_config, label, **_kwargs):
        if label == config.NATIVE_BRIDGE_LAUNCHD_LABEL:
            return LaunchdState(label, installed=True, loaded=True, override="enabled")
        return LaunchdState(label, installed=True, loaded=False, override="disabled")

    monkeypatch.setattr(lifecycle, "inspect_launchd", inspect)
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: f"/opt/om/bin/{name}")
    monkeypatch.setattr(
        cli_module,
        "_validate_llm_access",
        lambda _config: (_ for _ in ()).throw(AssertionError("provider validation must not run")),
    )

    result = CliRunner().invoke(cli, ["doctor", "--json", "--validate-key"])

    assert result.exit_code == 0, result.output
    checks = json.loads(result.output)
    assert next(row for row in checks if row["name"] == "Operating mode")["detail"] == "native-memory bridge (LLM-free)"
    assert next(row for row in checks if row["name"] == "Configured LLM access")["detail"].endswith(
        "no provider call made"
    )
    assert next(row for row in checks if row["name"] == "Native bridge legacy writers")["status"] == "PASS"
    assert next(row for row in checks if row["name"] == "Native bridge transcript hooks")["status"] == "PASS"
    forbidden_fixes = {"Run: om install --claude", "Run: om install --codex"}
    assert not forbidden_fixes.intersection(row["fix"] for row in checks)
    assert not any("--provider" in row["fix"] for row in checks)
