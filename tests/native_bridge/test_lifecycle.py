from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from observational_memory.config import Config
from observational_memory.native_bridge import lifecycle
from observational_memory.native_bridge.lifecycle import (
    LEGACY_WRITER_LABELS,
    NATIVE_BRIDGE_CODEX_ALLOWLIST,
    NATIVE_BRIDGE_INTERVAL_SECONDS,
    NativeBridgeLifecycleError,
    NativeBridgeSettings,
    atomic_write_owned_file,
    disable_bridge_launchd,
    discover_claude_projects,
    discover_codex_sources,
    install_bridge_launchd,
    launchd_override,
    launchd_plist_bytes,
    load_settings,
    purge_bridge_state,
    quiesce_legacy_launchd,
    restore_launchd_states,
    snapshot_owned_file,
    uninstall_bridge_launchd,
    write_settings,
)


def _config(monkeypatch, tmp_path: Path) -> Config:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Config(
        memory_dir=tmp_path / "data" / "observational-memory",
        env_file=tmp_path / "config" / "observational-memory" / "env",
        codex_home=tmp_path / "codex",
        claude_projects_dir=tmp_path / "claude" / "projects",
        search_backend="bm25",
    )


class FakeLaunchctl:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.disabled: dict[str, bool] = {}
        self.loaded: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.bootstrap_failures = 0

    @staticmethod
    def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, args):
        call = tuple(args)
        self.calls.append(call)
        operation = args[0]
        if operation == "print-disabled":
            rows = "\n".join(f'    "{label}" => {str(value).lower()}' for label, value in sorted(self.disabled.items()))
            return self._result(stdout=f"disabled services = {{\n{rows}\n}}\n")
        if operation == "print":
            label = args[1].rsplit("/", 1)[-1]
            if label in self.loaded:
                return self._result(stdout="service = loaded\n")
            return self._result(1, stderr="Could not find service")
        if operation in {"enable", "disable"}:
            label = args[1].rsplit("/", 1)[-1]
            self.disabled[label] = operation == "disable"
            return self._result()
        if operation == "bootout":
            label = args[1].rsplit("/", 1)[-1]
            self.loaded.discard(label)
            return self._result()
        if operation == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return self._result(5, stderr="injected bootstrap failure")
            payload = plistlib.loads(Path(args[2]).read_bytes())
            self.loaded.add(payload["Label"])
            return self._result()
        raise AssertionError(f"unexpected launchctl call: {args}")


def test_settings_round_trip_is_canonical_private_and_fixed(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    settings = NativeBridgeSettings(("project-b", "project-a", "project-a"))

    write_settings(config, settings)

    assert load_settings(config) == NativeBridgeSettings(("project-a", "project-b"))
    assert stat.S_IMODE(config.native_bridge_config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.native_bridge_config_path.stat().st_mode) == 0o600
    payload = json.loads(config.native_bridge_config_path.read_bytes())
    assert payload["codex_allowlist"] == list(NATIVE_BRIDGE_CODEX_ALLOWLIST)
    assert payload["interval_seconds"] == NATIVE_BRIDGE_INTERVAL_SECONDS


def test_settings_fail_closed_on_allowlist_or_permission_change(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    write_settings(config, NativeBridgeSettings(("project-a",)))
    payload = json.loads(config.native_bridge_config_path.read_bytes())
    payload["codex_allowlist"] = ["sessions/raw.md"]
    config.native_bridge_config_path.write_text(json.dumps(payload))
    config.native_bridge_config_path.chmod(0o600)

    with pytest.raises(NativeBridgeLifecycleError, match="allowlist|canonical"):
        load_settings(config)

    write_settings(config, NativeBridgeSettings(("project-a",)))
    config.native_bridge_config_path.chmod(0o644)
    with pytest.raises(NativeBridgeLifecycleError, match="0600"):
        load_settings(config)


@pytest.mark.parametrize("project", ["project\x00name", "project\nname", "project\tname", "project\x7fname"])
def test_project_selection_rejects_c0_and_del(project):
    with pytest.raises(NativeBridgeLifecycleError, match="project names"):
        NativeBridgeSettings((project,))


def test_settings_expected_snapshot_rejects_concurrent_change(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    write_settings(config, NativeBridgeSettings(("old-project",)))
    expected = snapshot_owned_file(config.native_bridge_config_path)
    concurrent = b'{"concurrent":true}\n'
    config.native_bridge_config_path.write_bytes(concurrent)
    config.native_bridge_config_path.chmod(0o600)

    with pytest.raises(NativeBridgeLifecycleError, match="concurrently changed"):
        write_settings(
            config,
            NativeBridgeSettings(("new-project",)),
            expected_snapshot=expected,
        )

    assert config.native_bridge_config_path.read_bytes() == concurrent


def test_atomic_owned_write_rejects_concurrent_hook_change(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    path = config.claude_settings_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"hooks":{}}\n')
    expected = snapshot_owned_file(path)
    concurrent = b'{"hooks":{},"unrelated":true}\n'
    path.write_bytes(concurrent)

    with pytest.raises(NativeBridgeLifecycleError, match="concurrently changed"):
        atomic_write_owned_file(
            path,
            b'{"hooks":{"SessionEnd":[]}}\n',
            expected_snapshot=expected,
        )

    assert path.read_bytes() == concurrent


def test_source_discovery_lists_names_and_counts_without_auto_enrollment(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    first = config.claude_projects_dir / "-Users-example-project"
    second = config.claude_projects_dir / "-Users-example-other"
    (first / "memory").mkdir(parents=True)
    (second / "memory").mkdir(parents=True)
    (first / "memory" / "MEMORY.md").write_text("private sentinel")
    (first / "memory" / "topic.md").write_text("another private sentinel")
    (first / "memory" / "raw_memories.md").write_text("excluded")
    (second / "memory" / "notes.txt").write_text("excluded")

    discovered = discover_claude_projects(config)

    assert discovered == ({"project": "-Users-example-project", "eligible_markdown_files": 2},)
    assert "sentinel" not in json.dumps(discovered)


def test_source_discovery_counts_only_secure_regular_user_files(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    memory = config.claude_projects_dir / "project-a" / "memory"
    memory.mkdir(parents=True)
    (memory / "valid.md").write_text("eligible")
    (memory / "directory.md").mkdir()
    (memory / "writable.md").write_text("unsafe")
    (memory / "writable.md").chmod(0o622)
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (memory / "symlink.md").symlink_to(outside)
    hardlink_source = tmp_path / "hardlink-source.md"
    hardlink_source.write_text("linked")
    os.link(hardlink_source, memory / "hardlink.md")

    discovered = discover_claude_projects(config)

    assert discovered == ({"project": "project-a", "eligible_markdown_files": 1},)


def test_source_discovery_rejects_group_or_world_writable_selected_directories(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    project = config.claude_projects_dir / "unsafe-project"
    memory = project / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("not eligible through an unsafe directory")
    project.chmod(0o777)

    assert discover_claude_projects(config) == ()


def test_codex_source_discovery_reports_fixed_presence_without_content(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    memories = config.codex_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("private codex sentinel")

    discovered = discover_codex_sources(config)

    assert discovered == (
        {"filename": "MEMORY.md", "present": True},
        {"filename": "memory_summary.md", "present": False},
    )
    assert "sentinel" not in json.dumps(discovered)


def test_codex_source_discovery_rejects_unsafe_allowlisted_leaf(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    memories = config.codex_home / "memories"
    memories.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    (memories / "MEMORY.md").symlink_to(outside)

    assert discover_codex_sources(config) == (
        {"filename": "MEMORY.md", "present": False},
        {"filename": "memory_summary.md", "present": False},
    )


def test_launchd_plist_uses_installed_om_fixed_cadence_and_no_provider_env(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)

    payload = plistlib.loads(launchd_plist_bytes(config, "/opt/om/bin/om"))

    assert payload["Label"] == config.NATIVE_BRIDGE_LAUNCHD_LABEL
    assert payload["ProgramArguments"] == ["/opt/om/bin/om", "native-bridge-worker"]
    assert payload["StartInterval"] == 900
    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] is False
    assert payload["Umask"] == 0o77
    assert not any("PROVIDER" in key or "API_KEY" in key for key in payload["EnvironmentVariables"])


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('disabled services = {\n    "label" => true\n}\n', "disabled"),
        ('disabled services = {\n    "label" => false\n}\n', "enabled"),
        ("disabled services = {\n}\n", "default"),
    ],
)
def test_disabled_state_parser_is_exact(monkeypatch, output, expected):
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")

    def run(_args):
        return subprocess.CompletedProcess([], 0, output, "")

    assert launchd_override("label", run_launchctl=run) == (expected, None)


def test_install_enables_before_bootstrap_and_verifies_loaded(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    fake = FakeLaunchctl(config)

    install_bridge_launchd(config, "/opt/om/bin/om", run_launchctl=fake)

    label = config.NATIVE_BRIDGE_LAUNCHD_LABEL
    assert config.native_bridge_launchd_plist_path.exists()
    assert stat.S_IMODE(config.native_bridge_launchd_plist_path.stat().st_mode) == 0o600
    assert fake.disabled[label] is False
    assert label in fake.loaded
    enable_index = fake.calls.index(("enable", f"gui/{os.getuid()}/{label}"))
    bootstrap_index = next(index for index, call in enumerate(fake.calls) if call[0] == "bootstrap")
    assert enable_index < bootstrap_index


def test_bootstrap_failure_restores_prior_plist_mode_and_loaded_state(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    config.native_bridge_launchd_plist_path.parent.mkdir(parents=True)
    prior_payload = plistlib.dumps({"Label": config.NATIVE_BRIDGE_LAUNCHD_LABEL, "ProgramArguments": ["old"]})
    config.native_bridge_launchd_plist_path.write_bytes(prior_payload)
    config.native_bridge_launchd_plist_path.chmod(0o640)
    fake = FakeLaunchctl(config)
    fake.disabled[config.NATIVE_BRIDGE_LAUNCHD_LABEL] = False
    fake.loaded.add(config.NATIVE_BRIDGE_LAUNCHD_LABEL)
    fake.bootstrap_failures = 1

    with pytest.raises(NativeBridgeLifecycleError, match="activation failed"):
        install_bridge_launchd(config, "/opt/om/bin/om", run_launchctl=fake)

    assert config.native_bridge_launchd_plist_path.read_bytes() == prior_payload
    assert stat.S_IMODE(config.native_bridge_launchd_plist_path.stat().st_mode) == 0o640
    assert config.NATIVE_BRIDGE_LAUNCHD_LABEL in fake.loaded


def test_bootstrap_failure_from_default_enters_safe_disabled_hold(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    fake = FakeLaunchctl(config)
    fake.bootstrap_failures = 1

    with pytest.raises(NativeBridgeLifecycleError, match="activation failed"):
        install_bridge_launchd(config, "/opt/om/bin/om", run_launchctl=fake)

    label = config.NATIVE_BRIDGE_LAUNCHD_LABEL
    assert fake.disabled[label] is True
    assert label not in fake.loaded
    override_calls = [call[0] for call in fake.calls if call[0] in {"enable", "disable"}]
    assert override_calls[-1] == "disable"


def test_plist_expected_snapshot_preserves_concurrent_replacement(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    fake = FakeLaunchctl(config)
    path = config.native_bridge_launchd_plist_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prior")
    path.chmod(0o600)
    concurrent = b"concurrent unrelated plist edit"
    original_write = lifecycle.atomic_write_owned_file

    def race(selected, data, *, mode=0o600, expected_snapshot=None):
        assert expected_snapshot is not None
        selected.write_bytes(concurrent)
        return original_write(
            selected,
            data,
            mode=mode,
            expected_snapshot=expected_snapshot,
        )

    monkeypatch.setattr(lifecycle, "atomic_write_owned_file", race)

    with pytest.raises(NativeBridgeLifecycleError, match="activation failed"):
        install_bridge_launchd(config, "/opt/om/bin/om", run_launchctl=fake)

    assert path.read_bytes() == concurrent
    assert fake.disabled[config.NATIVE_BRIDGE_LAUNCHD_LABEL] is True


def test_disable_retains_files_and_unloads_service(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    config.native_bridge_launchd_plist_path.parent.mkdir(parents=True)
    config.native_bridge_launchd_plist_path.write_bytes(launchd_plist_bytes(config, "/opt/om/bin/om"))
    write_settings(config, NativeBridgeSettings(("project-a",)))
    config.native_bridge_data_dir.mkdir(parents=True)
    receipt = config.native_bridge_data_dir / "receipt"
    receipt.write_text("keep")
    fake = FakeLaunchctl(config)
    fake.loaded.add(config.NATIVE_BRIDGE_LAUNCHD_LABEL)

    disable_bridge_launchd(config, run_launchctl=fake)

    assert config.native_bridge_launchd_plist_path.exists()
    assert config.native_bridge_config_path.exists()
    assert receipt.read_text() == "keep"
    assert fake.disabled[config.NATIVE_BRIDGE_LAUNCHD_LABEL] is True
    assert config.NATIVE_BRIDGE_LAUNCHD_LABEL not in fake.loaded


def test_uninstall_removes_only_bridge_plist_and_preserves_private_state(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    config.launch_agents_dir.mkdir(parents=True)
    config.native_bridge_launchd_plist_path.write_bytes(launchd_plist_bytes(config, "/opt/om/bin/om"))
    unrelated = config.launch_agents_dir / "com.example.unrelated.plist"
    unrelated.write_text("keep")
    legacy = config.codex_observe_launchd_plist_path
    legacy.write_text("legacy")
    write_settings(config, NativeBridgeSettings(("project-a",)))
    config.native_bridge_data_dir.mkdir(parents=True)
    fake = FakeLaunchctl(config)
    fake.loaded.add(config.NATIVE_BRIDGE_LAUNCHD_LABEL)

    uninstall_bridge_launchd(config, run_launchctl=fake)

    assert not config.native_bridge_launchd_plist_path.exists()
    assert unrelated.read_text() == "keep"
    assert legacy.read_text() == "legacy"
    assert config.native_bridge_config_path.exists()
    assert config.native_bridge_data_dir.exists()
    assert fake.disabled[config.NATIVE_BRIDGE_LAUNCHD_LABEL] is False


def test_purge_removes_only_private_bridge_state_and_exact_logs(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    write_settings(config, NativeBridgeSettings(("project-a",)))
    config.native_bridge_data_dir.mkdir(parents=True, mode=0o700)
    config.native_bridge_data_dir.chmod(0o700)
    (config.native_bridge_data_dir / "receipt.json").write_text("private")
    config.scheduler_log_dir.mkdir(parents=True)
    config.native_bridge_launchd_stdout_path.write_text("stdout")
    config.native_bridge_launchd_stderr_path.write_text("stderr")
    unrelated = config.scheduler_log_dir / "reflect.out.log"
    unrelated.write_text("keep")

    removed = purge_bridge_state(config)

    assert set(removed) == {
        config.native_bridge_config_dir,
        config.native_bridge_data_dir,
        config.native_bridge_launchd_stdout_path,
        config.native_bridge_launchd_stderr_path,
    }
    assert not config.native_bridge_config_dir.exists()
    assert not config.native_bridge_data_dir.exists()
    assert not config.native_bridge_launchd_stdout_path.exists()
    assert not config.native_bridge_launchd_stderr_path.exists()
    assert unrelated.read_text() == "keep"


def test_purge_refuses_unsafe_root_before_removing_any_state(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    write_settings(config, NativeBridgeSettings(("project-a",)))
    config.native_bridge_data_dir.mkdir(parents=True, mode=0o700)
    config.native_bridge_data_dir.chmod(0o755)

    with pytest.raises(NativeBridgeLifecycleError, match="unsafe native bridge purge root"):
        purge_bridge_state(config)

    assert config.native_bridge_config_path.exists()
    assert config.native_bridge_data_dir.exists()


def test_bridge_activation_quiesces_exactly_four_legacy_launchagents(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    fake = FakeLaunchctl(config)
    fake.loaded.update(LEGACY_WRITER_LABELS)

    snapshots = quiesce_legacy_launchd(config, run_launchctl=fake)

    assert set(snapshots) == set(LEGACY_WRITER_LABELS)
    assert all(fake.disabled[label] is True for label in LEGACY_WRITER_LABELS)
    assert not fake.loaded.intersection(LEGACY_WRITER_LABELS)


def test_restore_default_and_unknown_overrides_never_enables(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    fake = FakeLaunchctl(config)
    states = {
        "default-label": lifecycle.LaunchdState("default-label", False, False, "default"),
        "unknown-label": lifecycle.LaunchdState("unknown-label", False, False, "unknown"),
    }

    restore_launchd_states(config, states, run_launchctl=fake)

    assert fake.disabled == {"default-label": True, "unknown-label": True}
    assert not [call for call in fake.calls if call[0] == "enable"]


def test_non_macos_install_fails_before_launchctl_or_file_mutation(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    called = False

    def run(_args):
        nonlocal called
        called = True
        raise AssertionError("launchctl must not run")

    with pytest.raises(NativeBridgeLifecycleError, match="only supported on macOS"):
        install_bridge_launchd(config, "/opt/om/bin/om", run_launchctl=run)

    assert called is False
    assert not config.native_bridge_launchd_plist_path.exists()
