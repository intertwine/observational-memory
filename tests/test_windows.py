"""Windows-compatibility tests.

These tests exercise the Windows-specific code paths from non-Windows hosts
by monkeypatching ``sys.platform`` and ``os.environ``. The implementation
is structured so the same logic runs on real Windows machines without
additional platform-specific test infrastructure.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from observational_memory import config as config_module
from observational_memory.cli import (
    _claude_hook_commands,
    _resolve_scheduler_mode,
    _schtasks_argv_to_command,
    _schtasks_job_keys_for_targets,
    _schtasks_job_specs,
    cli,
)
from observational_memory.config import Config


@pytest.fixture
def windows_env(monkeypatch, tmp_path):
    """Simulate a Windows environment with isolated APPDATA / LOCALAPPDATA dirs."""
    appdata = tmp_path / "AppData" / "Roaming"
    local_appdata = tmp_path / "AppData" / "Local"
    home = tmp_path / "home"
    for p in (appdata, local_appdata, home):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for key in [
        "OM_LLM_PROVIDER",
        "OM_LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OM_VERTEX_PROJECT_ID",
        "OM_VERTEX_REGION",
        "OM_BEDROCK_REGION",
        "AWS_REGION",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "win32")
    monkeypatch.setattr("observational_memory.config.sys.platform", "win32")
    monkeypatch.setattr(config_module, "is_windows", lambda: True)

    return {"appdata": appdata, "local_appdata": local_appdata, "home": home}


# --- Path resolution ---


def test_data_home_uses_localappdata_on_windows(windows_env):
    config = Config()
    expected_root = windows_env["local_appdata"]
    assert config.memory_dir == expected_root / "observational-memory"


def test_config_home_uses_appdata_on_windows(windows_env):
    config = Config()
    expected_root = windows_env["appdata"]
    assert config.env_file == expected_root / "observational-memory" / "env"


def test_cowork_paths_use_appdata_on_windows(windows_env):
    config = Config()
    appdata = windows_env["appdata"]
    assert config.cowork_sessions_dir == appdata / "Claude" / "local-agent-mode-sessions"
    assert config.cowork_plugins_dir == appdata / "Claude" / "local-agent-mode-plugins"


def test_xdg_overrides_take_precedence_over_appdata(windows_env, monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(explicit))
    config = Config()
    assert config.memory_dir == explicit / "observational-memory"


def test_ensure_env_file_skips_chmod_on_windows(windows_env):
    config = Config()
    assert config.ensure_env_file() is True
    assert config.env_file.exists()
    # No assertion on permission bits — chmod 600 is a no-op on Windows and
    # the file should exist regardless of mode.


# --- Scheduler resolution ---


def test_resolve_scheduler_auto_picks_schtasks_on_windows():
    assert _resolve_scheduler_mode("auto", None, platform="win32") == "schtasks"


def test_resolve_scheduler_rejects_launchd_on_windows():
    with pytest.raises(Exception):
        _resolve_scheduler_mode("launchd", None, platform="win32")


def test_resolve_scheduler_rejects_cron_on_windows():
    with pytest.raises(Exception):
        _resolve_scheduler_mode("cron", None, platform="win32")


def test_resolve_scheduler_rejects_schtasks_off_windows():
    with pytest.raises(Exception):
        _resolve_scheduler_mode("schtasks", None, platform="darwin")
    with pytest.raises(Exception):
        _resolve_scheduler_mode("schtasks", None, platform="linux")


# --- schtasks task specs ---


def test_schtasks_specs_codex_target_includes_shared_jobs(windows_env):
    # Mirrors the cron behavior: a `--codex` install also schedules the shared
    # auto-memory + reflect jobs that any agent benefits from.
    config = Config(memory_dir=windows_env["local_appdata"] / "obs")
    specs = _schtasks_job_specs(config, "codex", om_path="C:/tools/om.exe")
    keys = [s["key"] for s in specs]
    assert set(keys) == {"codex", "claude-memory", "reflect"}
    codex = next(s for s in specs if s["key"] == "codex")
    assert codex["argv"] == ["C:/tools/om.exe", "observe", "--source", "codex"]
    assert codex["schedule_kind"] == "minute"


def test_schtasks_specs_both_targets_include_reflect_daily(windows_env):
    config = Config(memory_dir=windows_env["local_appdata"] / "obs")
    specs = _schtasks_job_specs(config, "both", om_path="C:/tools/om.exe")
    keys = [s["key"] for s in specs]
    assert set(keys) == {"codex", "claude-memory", "reflect"}

    reflect = next(s for s in specs if s["key"] == "reflect")
    assert reflect["schedule_kind"] == "daily"
    assert reflect["schedule_time"] == "04:00"


def test_schtasks_job_keys_for_targets_match_cron_keys():
    # Matches `_cron_job_keys_for_targets` so both backends include the same
    # set of OM-managed jobs for each install target.
    assert _schtasks_job_keys_for_targets("codex") == {"codex", "claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("claude") == {"claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("both") == {"codex", "claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("cowork") == set()


def test_schtasks_argv_quoting_handles_spaces():
    cmd = _schtasks_argv_to_command(["C:/Program Files/om/om.exe", "observe", "--source", "codex"])
    assert '"C:/Program Files/om/om.exe"' in cmd
    assert "observe --source codex" in cmd


# --- Install flow on Windows ---


def test_install_auto_on_windows_invokes_schtasks(windows_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "observational_memory.cli._install_schtasks",
        lambda config, targets: calls.append(("schtasks", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._install_launchd",
        lambda config, targets: calls.append(("launchd", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._install_cron",
        lambda config, targets: calls.append(("cron", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("schtasks", "codex")]


def test_install_cowork_skips_on_windows(windows_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "install",
            "--cowork",
            "--scheduler",
            "none",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "only supported on macOS" in result.output
    appdata = windows_env["appdata"]
    plugin_dir = appdata / "Claude" / "local-agent-mode-plugins" / "observational-memory"
    assert not plugin_dir.exists()


def test_install_claude_uses_om_commands_on_windows(windows_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "observational_memory.cli._find_om_path",
        lambda: "C:/tools/om.exe",
    )
    monkeypatch.setattr(
        "observational_memory.cli._install_schtasks",
        lambda config, targets: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "install",
            "--claude",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    settings_path = windows_env["home"] / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    session_start_cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    checkpoint_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert session_start_cmd.endswith(" context")
    assert checkpoint_cmd.endswith(" claude-checkpoint")
    # No bash hook scripts are referenced.
    assert ".sh" not in session_start_cmd
    assert ".sh" not in checkpoint_cmd


def test_claude_hook_commands_on_windows_use_om_directly(windows_env, monkeypatch):
    monkeypatch.setattr(
        "observational_memory.cli._find_om_path",
        lambda: "C:/tools/om.exe",
    )
    session_start_cmd, checkpoint_cmd = _claude_hook_commands()
    assert session_start_cmd.endswith(" context")
    assert checkpoint_cmd.endswith(" claude-checkpoint")
    assert "om.exe" in session_start_cmd
    assert "om.exe" in checkpoint_cmd


# --- claude-checkpoint command ---


def test_claude_checkpoint_runs_observer_when_transcript_exists(windows_env, monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"},"uuid":"a","timestamp":"2026-05-09T00:00:00Z"}\n')

    calls: list = []

    def fake_observe(transcript_path, config, dry_run):
        calls.append(("observe", transcript_path, dry_run))

    def fake_catchup(config):
        calls.append(("catchup",))

    monkeypatch.setattr(
        "observational_memory.observe.observe_claude_transcript",
        fake_observe,
    )
    monkeypatch.setattr(
        "observational_memory.cli._maybe_run_reflector_catchup",
        fake_catchup,
    )

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "SessionEnd"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)

    assert result.exit_code == 0, result.output
    assert ("observe", transcript, False) in calls
    assert ("catchup",) in calls


def test_claude_checkpoint_respects_disable_env(windows_env, monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setenv("OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS", "1")

    calls: list = []

    monkeypatch.setattr(
        "observational_memory.observe.observe_claude_transcript",
        lambda *a, **k: calls.append("observe"),
    )

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "UserPromptSubmit"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)

    assert result.exit_code == 0
    assert calls == []


def test_claude_checkpoint_handles_missing_transcript(windows_env):
    runner = CliRunner()
    payload = json.dumps({"transcript_path": "/nonexistent/path.jsonl"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)
    assert result.exit_code == 0


# --- Uninstall ---


def test_uninstall_on_windows_removes_schtasks_not_cron(windows_env, monkeypatch):
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_schtasks",
        lambda config, targets="both": calls.append(("schtasks", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_cron",
        lambda targets="both": calls.append(("cron", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_launchd",
        lambda config, targets="both": calls.append(("launchd", targets)),
    )

    result = runner.invoke(cli, ["uninstall", "--codex"])
    assert result.exit_code == 0, result.output

    # On Windows we expect schtasks to be invoked and cron to be skipped.
    kinds = [name for name, _ in calls]
    assert "schtasks" in kinds
    assert "cron" not in kinds
