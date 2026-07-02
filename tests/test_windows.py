"""Windows-compatibility tests.

These tests exercise the Windows-specific code paths from non-Windows hosts
by monkeypatching ``sys.platform`` and ``os.environ``. The implementation
is structured so the same logic runs on real Windows machines without
additional platform-specific test infrastructure.
"""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from observational_memory import config as config_module
from observational_memory.cli import (
    ObserverWorkerTimeout,
    _claude_hook_commands,
    _resolve_scheduler_mode,
    _run_bounded_observer_call,
    _run_with_process_timeout,
    _schtasks_argv_to_command,
    _schtasks_job_keys_for_targets,
    _schtasks_job_specs,
    cli,
)
from observational_memory.config import Config
from observational_memory.sync.config import load_cluster_config


def _sleep_past_process_timeout() -> None:
    time.sleep(5)


def _return_observer_value(_config: Config, value: int) -> int:
    return value


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

    # ``shutil.which`` on Python 3.13 dispatches into ``_winapi`` when
    # ``sys.platform == 'win32'``; faking the platform on a POSIX runner
    # raises AttributeError. Stub ``_find_om_path`` to a stable path so
    # the install flow doesn't take that codepath under test.
    monkeypatch.setattr(
        "observational_memory.cli._find_om_path",
        lambda: "C:/tools/om.exe",
    )

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


def test_cluster_init_expands_windows_transport_env_path(windows_env):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cluster",
            "init",
            "--name",
            "Win Cluster",
            "--node-alias",
            "win-node",
            "--transport",
            r"filesystem:%LOCALAPPDATA%\OM\cluster",
        ],
    )

    assert result.exit_code == 0, result.output
    cluster_config = load_cluster_config(Config())
    assert cluster_config is not None
    assert cluster_config.transports[0].path == str(windows_env["local_appdata"]) + r"\OM\cluster"


def test_doctor_warns_for_windows_cluster_key_acl_verification(windows_env, monkeypatch):
    monkeypatch.setattr(
        "observational_memory.cli.shutil.which",
        lambda name: "C:/tools/om.exe" if name == "om" else None,
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cluster",
            "init",
            "--name",
            "Win Cluster",
            "--node-alias",
            "win-node",
            "--transport",
            r"filesystem:C:\Users\Bryan\Sync\om-cluster",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    checks = json.loads(result.stdout)
    key_check = next(item for item in checks if item["name"] == "OM Cluster key permissions")
    assert key_check["status"] == "WARN"
    assert "Windows ACL owner-only verification" in key_check["detail"]


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
    assert codex["argv"] == ["C:/tools/om.exe", "observe-worker", "--source", "codex"]
    assert codex["schedule_kind"] == "minute"


def test_schtasks_specs_both_targets_include_reflect_daily(windows_env):
    config = Config(memory_dir=windows_env["local_appdata"] / "obs")
    specs = _schtasks_job_specs(config, "both", om_path="C:/tools/om.exe")
    keys = [s["key"] for s in specs]
    assert set(keys) == {"codex", "claude", "claude-memory", "reflect"}

    claude = next(s for s in specs if s["key"] == "claude")
    assert claude["argv"] == ["C:/tools/om.exe", "observe-worker", "--source", "claude"]
    assert claude["schedule_kind"] == "minute"
    reflect = next(s for s in specs if s["key"] == "reflect")
    assert reflect["schedule_kind"] == "daily"
    assert reflect["schedule_time"] == "04:00"


def test_schtasks_job_keys_for_targets_match_cron_keys():
    # Matches `_cron_job_keys_for_targets` so both backends include the same
    # set of OM-managed jobs for each install target.
    assert _schtasks_job_keys_for_targets("codex") == {"codex", "claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("claude") == {"claude", "claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("both") == {"codex", "claude", "claude-memory", "reflect"}
    assert _schtasks_job_keys_for_targets("cowork") == set()


def test_schtasks_argv_quoting_handles_spaces():
    cmd = _schtasks_argv_to_command(["C:/Program Files/om/om.exe", "observe", "--source", "codex"])
    assert '"C:/Program Files/om/om.exe"' in cmd
    assert "observe --source codex" in cmd


def test_process_timeout_terminates_hung_child():
    with pytest.raises(ObserverWorkerTimeout):
        _run_with_process_timeout(_sleep_past_process_timeout, 0.1)


def test_bounded_observer_uses_process_timeout_on_windows(windows_env, monkeypatch):
    config = Config(memory_dir=windows_env["local_appdata"] / "observational-memory")
    calls = []

    def fake_process_timeout(fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs):
        calls.append((fn, timeout_seconds, max_rss_bytes, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setenv("OM_OBSERVER_WORKER_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("OM_OBSERVER_WORKER_MAX_RSS_MB", "64")
    monkeypatch.setattr("observational_memory.cli._run_with_process_timeout", fake_process_timeout)

    result = _run_bounded_observer_call(config, _return_observer_value, config, 42)

    assert result == 42
    assert calls == [(_return_observer_value, 17, 64 * 1024 * 1024, (config, 42), {})]


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


def test_claude_hook_commands_on_windows_use_doublequote_for_paths_with_spaces(windows_env, monkeypatch):
    """cmd.exe treats single quotes as literal characters; paths must be wrapped in double quotes."""
    monkeypatch.setattr(
        "observational_memory.cli._find_om_path",
        lambda: r"C:\Users\First Last\AppData\Local\bin\om.exe",
    )
    session_start_cmd, checkpoint_cmd = _claude_hook_commands()
    # Double-quoted, not single-quoted (POSIX shlex.quote would have produced
    # a single-quoted string that cmd.exe treats as literal characters).
    assert session_start_cmd.startswith(r'"C:\Users\First Last\AppData\Local\bin\om.exe"')
    assert checkpoint_cmd.startswith(r'"C:\Users\First Last\AppData\Local\bin\om.exe"')
    assert "'" not in session_start_cmd
    assert "'" not in checkpoint_cmd
    # And shlex.split (used by uninstall idempotency) recovers the path.
    import shlex

    parts = shlex.split(session_start_cmd)
    assert parts[0] == r"C:\Users\First Last\AppData\Local\bin\om.exe"
    assert parts[1] == "context"


def test_codex_hook_commands_on_windows_use_doublequote_for_paths_with_spaces(windows_env, monkeypatch):
    """The Codex hook builders must use the same Windows-friendly quoting as Claude."""
    from observational_memory.cli import (
        _build_codex_checkpoint_command,
        _build_codex_session_start_command,
    )

    monkeypatch.setattr(
        "observational_memory.cli._find_om_path",
        lambda: r"C:\Program Files\om\om.exe",
    )

    start_cmd = _build_codex_session_start_command()
    stop_cmd = _build_codex_checkpoint_command()
    assert start_cmd.startswith(r'"C:\Program Files\om\om.exe"')
    assert stop_cmd.startswith(r'"C:\Program Files\om\om.exe"')
    assert "'" not in start_cmd
    assert "'" not in stop_cmd


# --- claude-checkpoint command ---


def _claude_transcript_line(uuid: str, content: str = "hi") -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": content},
            "uuid": uuid,
            "timestamp": "2026-05-09T00:00:00Z",
        }
    )


def test_claude_checkpoint_spawns_worker_for_force_event(windows_env, monkeypatch, tmp_path):
    """SessionEnd events must skip throttling and spawn a detached worker."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")

    spawned: list[tuple[list[str], str | None]] = []

    def fake_spawn(argv, cwd=None):
        spawned.append((list(argv), cwd))

    monkeypatch.setattr("observational_memory.cli._spawn_detached", fake_spawn)

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "SessionEnd"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)

    assert result.exit_code == 0, result.output
    assert len(spawned) == 1
    argv, _ = spawned[0]
    assert argv[1:] == ["claude-checkpoint-worker", "--transcript", str(transcript)]


def test_claude_checkpoint_throttles_on_interval(windows_env, monkeypatch, tmp_path):
    """Within OM_SESSION_OBSERVER_INTERVAL_SECONDS, in-session events are skipped."""
    from datetime import datetime, timezone

    from observational_memory.cli import _update_checkpoint_state

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")

    config = Config(memory_dir=windows_env["local_appdata"] / "observational-memory")
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    # Pretend we observed this transcript 1 second ago at an older message count.
    _update_checkpoint_state(
        config.claude_checkpoint_state_path,
        transcript,
        message_count=0,
        status="success",
    )

    spawned: list = []
    monkeypatch.setattr(
        "observational_memory.cli._spawn_detached",
        lambda argv, cwd=None: spawned.append((argv, cwd)),
    )

    monkeypatch.setenv("OM_SESSION_OBSERVER_INTERVAL_SECONDS", "900")

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "UserPromptSubmit"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)
    assert result.exit_code == 0, result.output
    # Most recent observation is within the throttle interval AND message
    # count hasn't grown (state has 0, transcript has 1) → message count
    # check passes but the time-based throttle short-circuits.
    # Burn down the message-count skip first by showing that the count
    # difference *would* allow it but the timestamp throttles instead.
    # Because state has count 0 < current 1, the message-count skip
    # doesn't trigger — only the interval throttle blocks the spawn.
    assert spawned == []
    # Sanity: a force event still runs even with throttling state in place.
    state_then = _update_checkpoint_state  # alias to silence unused linters
    _ = state_then
    runner2 = CliRunner()
    force = json.dumps({"transcript_path": str(transcript), "hook_event_name": "SessionEnd"})
    result2 = runner2.invoke(cli, ["claude-checkpoint"], input=force)
    assert result2.exit_code == 0, result2.output
    # Force event always advances past the throttle.
    assert len(spawned) == 1

    # And confirm a present-day timestamp lives inside the interval window.
    now = datetime.now(timezone.utc).timestamp()
    state = json.loads(config.claude_checkpoint_state_path.read_text())
    last = state[str(transcript)]["last_observed"]
    assert now - last < 900


def test_claude_checkpoint_skips_when_message_count_unchanged(windows_env, monkeypatch, tmp_path):
    """If message count hasn't grown since the last observation, skip."""
    from observational_memory.cli import _update_checkpoint_state

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")

    config = Config(memory_dir=windows_env["local_appdata"] / "observational-memory")
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    # Pretend we already observed at the same message count.
    _update_checkpoint_state(
        config.claude_checkpoint_state_path,
        transcript,
        message_count=1,
        status="success",
    )

    spawned: list = []
    monkeypatch.setattr(
        "observational_memory.cli._spawn_detached",
        lambda argv, cwd=None: spawned.append(argv),
    )
    # Interval is 0 to ensure only the message-count gate is exercised.
    monkeypatch.setenv("OM_SESSION_OBSERVER_INTERVAL_SECONDS", "0")

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "UserPromptSubmit"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)
    assert result.exit_code == 0, result.output
    assert spawned == []


def test_claude_checkpoint_respects_disable_env(windows_env, monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")
    monkeypatch.setenv("OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS", "1")

    spawned: list = []
    monkeypatch.setattr(
        "observational_memory.cli._spawn_detached",
        lambda argv, cwd=None: spawned.append(argv),
    )

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "UserPromptSubmit"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)

    assert result.exit_code == 0
    assert spawned == []


def test_claude_checkpoint_handles_missing_transcript(windows_env):
    runner = CliRunner()
    payload = json.dumps({"transcript_path": "/nonexistent/path.jsonl"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)
    assert result.exit_code == 0


def test_claude_checkpoint_writes_state_and_holds_lock(windows_env, monkeypatch, tmp_path):
    """The checkpoint must persist state and hold the per-transcript lock."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")

    monkeypatch.setattr("observational_memory.cli._spawn_detached", lambda argv, cwd=None: 5151)

    config = Config(memory_dir=windows_env["local_appdata"] / "observational-memory")

    runner = CliRunner()
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "SessionEnd"})
    result = runner.invoke(cli, ["claude-checkpoint"], input=payload)
    assert result.exit_code == 0, result.output

    state = json.loads(config.claude_checkpoint_state_path.read_text())
    entry = state[str(transcript)]
    assert entry["status"] == "in_progress"
    assert entry["message_count"] == 1

    # The per-transcript lock dir should have been created and held — the
    # worker is responsible for releasing it, and we mocked that out.
    from observational_memory.cli import _checkpoint_lock_path

    lock_path = _checkpoint_lock_path(config.claude_checkpoint_lock_dir, transcript)
    assert lock_path.exists()
    assert (lock_path / "owner").read_text().startswith("pid=5151\n")


def test_claude_checkpoint_worker_runs_observer(windows_env, monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_transcript_line("a") + "\n")

    calls: list = []

    def fake_observe(transcript_path, config, dry_run):
        calls.append(("observe", transcript_path, dry_run))

    monkeypatch.setattr(
        "observational_memory.observe.observe_claude_transcript",
        fake_observe,
    )
    monkeypatch.setattr(
        "observational_memory.cli._maybe_run_reflector_catchup",
        lambda config: calls.append(("catchup",)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._run_with_process_timeout",
        lambda fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs: fn(*args, **kwargs),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["claude-checkpoint-worker", "--transcript", str(transcript)])
    assert result.exit_code == 0, result.output
    assert ("observe", transcript, False) in calls
    assert ("catchup",) in calls


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
