"""Tests for observe CLI reflector catch-up behavior and Codex checkpoints."""

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from observational_memory.cli import (
    ObserverWorkerMemoryExceeded,
    ObserverWorkerTimeout,
    _acquire_codex_checkpoint_lock,
    _observer_worker_lock_stale_seconds,
    _observer_worker_max_rss_bytes,
    _release_codex_checkpoint_lock,
    _run_bounded_observer_call,
    _run_with_process_timeout,
    cli,
)
from observational_memory.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


def _sleep_briefly() -> None:
    time.sleep(5)


def _sleep_a_bit() -> None:
    time.sleep(1.2)


def _return_value(_config: Config, value: int) -> int:
    return value


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    for p in (home, xdg_config, xdg_data, codex_home):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_observe_runs_reflector_catchup_after_scan(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    calls = {"count": 0}

    monkeypatch.setattr("observational_memory.observe.observe_all_codex", lambda config, dry_run: [])
    monkeypatch.setattr("observational_memory.observe.observe_all_claude", lambda config, dry_run: [])

    def fake_catchup(config):
        calls["count"] += 1

    monkeypatch.setattr("observational_memory.cli._maybe_run_reflector_catchup", fake_catchup)

    result = runner.invoke(cli, ["observe", "--source", "codex"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 1


def test_observe_skips_reflector_catchup_in_dry_run(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    calls = {"count": 0}

    monkeypatch.setattr("observational_memory.observe.observe_all_codex", lambda config, dry_run: [])

    def fake_catchup(config):
        calls["count"] += 1

    monkeypatch.setattr("observational_memory.cli._maybe_run_reflector_catchup", fake_catchup)

    result = runner.invoke(cli, ["observe", "--source", "codex", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 0


def test_observe_transcript_routes_to_explicit_codex_source(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    calls = {"transcript": None, "dry_run": None}

    def fake_observe(transcript_path, config, dry_run):
        calls["transcript"] = transcript_path
        calls["dry_run"] = dry_run
        return "## Observations\n\n- test"

    monkeypatch.setattr("observational_memory.observe.observe_codex_transcript", fake_observe)

    result = runner.invoke(cli, ["observe", "--transcript", str(transcript), "--source", "codex", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["transcript"] == transcript
    assert calls["dry_run"] is True
    assert "Observations updated" in result.output


def test_observe_transcript_auto_detects_codex_session_paths(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    calls = {"count": 0}

    def fake_observe(transcript_path, config, dry_run):
        calls["count"] += 1
        assert transcript_path == transcript
        return None

    monkeypatch.setattr("observational_memory.observe.observe_codex_transcript", fake_observe)

    result = runner.invoke(cli, ["observe", "--transcript", str(transcript), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 1
    assert "No new messages to process." in result.output


def test_observe_transcript_routes_to_explicit_hermes_source(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "home" / ".hermes" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("hermes-session.jsonl").read_text())

    calls = {"transcript": None, "dry_run": None}

    def fake_observe(transcript_path, config, dry_run):
        calls["transcript"] = transcript_path
        calls["dry_run"] = dry_run
        return "## Observations\n\n- hermes"

    monkeypatch.setattr("observational_memory.observe.observe_hermes_transcript", fake_observe)

    result = runner.invoke(cli, ["observe", "--transcript", str(transcript), "--source", "hermes", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["transcript"] == transcript
    assert calls["dry_run"] is True
    assert "Observations updated" in result.output


def test_observe_transcript_auto_detects_hermes_session_paths(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "home" / ".hermes" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("hermes-session.jsonl").read_text())

    calls = {"count": 0}

    def fake_observe(transcript_path, config, dry_run):
        calls["count"] += 1
        assert transcript_path == transcript
        return None

    monkeypatch.setattr("observational_memory.observe.observe_hermes_transcript", fake_observe)

    result = runner.invoke(cli, ["observe", "--transcript", str(transcript), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 1
    assert "No new messages to process." in result.output


def test_observer_worker_timeout_escapes_exception_handlers():
    timeout = ObserverWorkerTimeout("boom")

    assert not isinstance(timeout, Exception)
    assert not isinstance(timeout, TimeoutError)


def test_observer_worker_lock_stale_default_tracks_timeout(monkeypatch):
    monkeypatch.delenv("OM_OBSERVER_WORKER_LOCK_STALE_SECONDS", raising=False)

    assert _observer_worker_lock_stale_seconds(timeout_seconds=300) == 360


def test_observer_worker_max_rss_bytes_parses_env(monkeypatch):
    monkeypatch.setenv("OM_OBSERVER_WORKER_MAX_RSS_MB", "128")

    assert _observer_worker_max_rss_bytes() == 128 * 1024 * 1024


def test_observer_worker_max_rss_bytes_falls_back_on_invalid_value(monkeypatch, capsys):
    monkeypatch.setenv("OM_OBSERVER_WORKER_MAX_RSS_MB", "abc")

    assert _observer_worker_max_rss_bytes() == 4096 * 1024 * 1024
    assert "OM_OBSERVER_WORKER_MAX_RSS_MB" in capsys.readouterr().err


def test_observer_worker_max_rss_bytes_falls_back_on_negative_value(monkeypatch, capsys):
    monkeypatch.setenv("OM_OBSERVER_WORKER_MAX_RSS_MB", "-5")

    assert _observer_worker_max_rss_bytes() == 4096 * 1024 * 1024
    assert "OM_OBSERVER_WORKER_MAX_RSS_MB" in capsys.readouterr().err


def test_process_timeout_terminates_child_over_memory_cap(monkeypatch):
    monkeypatch.setattr("observational_memory.cli._process_rss_bytes", lambda pid: 2 * 1024 * 1024)

    try:
        _run_with_process_timeout(_sleep_briefly, 5, max_rss_bytes=1024 * 1024)
    except ObserverWorkerMemoryExceeded as exc:
        assert "exceeded 1 MiB RSS" in str(exc)
    else:
        raise AssertionError("expected observer worker memory cap to terminate child")


def test_process_timeout_throttles_rss_sampling(monkeypatch):
    calls = {"count": 0}

    def fake_rss(pid):
        calls["count"] += 1
        return 1024  # far under the cap; child should run to completion

    monkeypatch.setattr("observational_memory.cli._process_rss_bytes", fake_rss)

    result = _run_with_process_timeout(_sleep_a_bit, 30, max_rss_bytes=1024 * 1024 * 1024)

    assert result is None
    # 0.25s joins over ~1.2s of child runtime would sample RSS ~5+ times with no
    # throttle; the 1s throttle should bring that down to roughly 2-3. Use a
    # generous upper bound to avoid CI flakiness while still catching a
    # regression back to per-iteration sampling.
    assert 1 <= calls["count"] <= 4


def test_bounded_observer_passes_memory_cap_to_process_timeout(monkeypatch, tmp_path):
    config = Config(memory_dir=tmp_path / "memory")
    calls = []

    def fake_process_timeout(fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs):
        calls.append((timeout_seconds, max_rss_bytes, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setenv("OM_OBSERVER_WORKER_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("OM_OBSERVER_WORKER_MAX_RSS_MB", "64")
    monkeypatch.setattr("observational_memory.cli._run_with_process_timeout", fake_process_timeout)

    result = _run_bounded_observer_call(config, _return_value, config, 42)

    assert result == 42
    assert calls == [(17, 64 * 1024 * 1024, (config, 42), {})]


def test_codex_checkpoint_spawns_worker_and_records_state(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    popen_calls = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))
            self.pid = 4242

    monkeypatch.setattr("observational_memory.cli._find_om_path", lambda: "/tmp/bin/om")
    monkeypatch.setattr("subprocess.Popen", DummyPopen)

    result = runner.invoke(
        cli,
        ["codex-checkpoint"],
        input=json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path)}),
    )

    assert result.exit_code == 0, result.output
    assert popen_calls == [
        (
            ["/tmp/bin/om", "codex-checkpoint-worker", "--transcript", str(transcript)],
            {
                "cwd": str(tmp_path),
                "stdin": -3,
                "stdout": -3,
                "stderr": -3,
                "start_new_session": True,
            },
        )
    ]

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    state = json.loads(config.codex_checkpoint_state_path.read_text())
    assert state[str(transcript)]["status"] == "in_progress"
    assert state[str(transcript)]["message_count"] == 7
    lock_paths = list(config.codex_checkpoint_lock_dir.iterdir())
    assert len(lock_paths) == 1
    assert (lock_paths[0] / "owner").read_text().startswith("pid=4242\n")


def test_codex_checkpoint_skips_when_message_count_is_already_current(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    config.codex_checkpoint_state_path.write_text(
        json.dumps({str(transcript): {"last_observed": 123, "message_count": 7, "status": "success"}})
    )

    popen_calls = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr("subprocess.Popen", DummyPopen)

    result = runner.invoke(
        cli,
        ["codex-checkpoint"],
        input=json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path)}),
    )

    assert result.exit_code == 0, result.output
    assert popen_calls == []
    assert config.codex_checkpoint_lock_dir.exists()
    assert list(config.codex_checkpoint_lock_dir.iterdir()) == []


def test_codex_checkpoint_worker_updates_state_and_releases_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    calls = {"observe": 0, "catchup": 0}

    def fake_observe(transcript_path, config, dry_run):
        calls["observe"] += 1
        assert transcript_path == transcript
        assert dry_run is False
        return "## Observations\n\n- test"

    def fake_catchup(config):
        calls["catchup"] += 1

    monkeypatch.setattr("observational_memory.observe.observe_codex_transcript", fake_observe)
    monkeypatch.setattr("observational_memory.cli._maybe_run_reflector_catchup", fake_catchup)
    monkeypatch.setattr(
        "observational_memory.cli._run_with_process_timeout",
        lambda fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs: fn(*args, **kwargs),
    )

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    lock_path = config.codex_checkpoint_lock_dir / "test-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("observational_memory.cli._codex_checkpoint_lock_path", lambda config, path: lock_path)

    result = runner.invoke(cli, ["codex-checkpoint-worker", "--transcript", str(transcript)])

    assert result.exit_code == 0, result.output
    assert calls == {"observe": 1, "catchup": 1}

    state = json.loads(config.codex_checkpoint_state_path.read_text())
    assert state[str(transcript)]["status"] == "success"
    assert not lock_path.exists()


def test_codex_checkpoint_worker_skips_when_observer_slot_is_busy(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    def fail_observe(*args, **kwargs):
        raise AssertionError("observer should not run while global slot is busy")

    monkeypatch.setattr("observational_memory.observe.observe_codex_transcript", fail_observe)

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    (config.memory_dir / ".observer-worker.lock").mkdir()
    lock_path = config.codex_checkpoint_lock_dir / "test-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("observational_memory.cli._codex_checkpoint_lock_path", lambda config, path: lock_path)

    result = runner.invoke(cli, ["codex-checkpoint-worker", "--transcript", str(transcript)])

    assert result.exit_code == 0, result.output
    state = json.loads(config.codex_checkpoint_state_path.read_text())
    assert state[str(transcript)]["status"] == "skipped_busy"
    assert not lock_path.exists()


def test_codex_checkpoint_worker_marks_timeout_and_releases_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    def fake_timeout(fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs):
        raise ObserverWorkerTimeout("boom")

    monkeypatch.setattr("observational_memory.cli._run_with_process_timeout", fake_timeout)

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    lock_path = config.codex_checkpoint_lock_dir / "test-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("observational_memory.cli._codex_checkpoint_lock_path", lambda config, path: lock_path)

    result = runner.invoke(cli, ["codex-checkpoint-worker", "--transcript", str(transcript)])

    assert result.exit_code == 0, result.output
    state = json.loads(config.codex_checkpoint_state_path.read_text())
    assert state[str(transcript)]["status"] == "timeout"
    assert not lock_path.exists()


def test_codex_checkpoint_worker_marks_memory_exceeded_and_releases_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    transcript = tmp_path / "codex" / "sessions" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(FIXTURES.joinpath("codex-transcript.jsonl").read_text())

    def fake_memory_exceeded(fn, timeout_seconds, *args, max_rss_bytes=None, **kwargs):
        raise ObserverWorkerMemoryExceeded("boom")

    monkeypatch.setattr("observational_memory.cli._run_with_process_timeout", fake_memory_exceeded)

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    lock_path = config.codex_checkpoint_lock_dir / "test-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("observational_memory.cli._codex_checkpoint_lock_path", lambda config, path: lock_path)

    result = runner.invoke(cli, ["codex-checkpoint-worker", "--transcript", str(transcript)])

    assert result.exit_code == 0, result.output
    state = json.loads(config.codex_checkpoint_state_path.read_text())
    assert state[str(transcript)]["status"] == "memory_exceeded"
    assert not lock_path.exists()


def test_observe_worker_skips_when_observer_slot_is_busy(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    def fail_scan(*args, **kwargs):
        raise AssertionError("scan should not run while global slot is busy")

    monkeypatch.setattr("observational_memory.observe.observe_all_codex", fail_scan)

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    (config.memory_dir / ".observer-worker.lock").mkdir()

    result = runner.invoke(cli, ["observe-worker", "--source", "codex"])

    assert result.exit_code == 0, result.output
    assert "already running" in result.output


def test_codex_checkpoint_reclaims_stale_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()

    lock_path = config.codex_checkpoint_lock_dir / "stale-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    stale_time = time.time() - 120
    os.utime(lock_path, (stale_time, stale_time))

    monkeypatch.setenv("OM_SESSION_OBSERVER_LOCK_STALE_MINUTES", "1")

    assert _acquire_codex_checkpoint_lock(config, lock_path) is True
    assert lock_path.exists()

    _release_codex_checkpoint_lock(lock_path)


def test_codex_checkpoint_reclaims_dead_owner_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()

    lock_path = config.codex_checkpoint_lock_dir / "dead-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    (lock_path / "owner").write_text("pid=999999\ncreated=123\n")

    def fake_kill(pid, sig):
        assert pid == 999999
        assert sig == 0
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", fake_kill)

    assert _acquire_codex_checkpoint_lock(config, lock_path) is True
    assert (lock_path / "owner").read_text().startswith(f"pid={os.getpid()}\n")

    _release_codex_checkpoint_lock(lock_path)


def test_codex_checkpoint_does_not_reclaim_live_owner_lock(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()

    lock_path = config.codex_checkpoint_lock_dir / "live-lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    (lock_path / "owner").write_text("pid=12345\ncreated=123\n")
    stale_time = time.time() - 120
    os.utime(lock_path, (stale_time, stale_time))

    def fake_kill(pid, sig):
        assert pid == 12345
        assert sig == 0

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setenv("OM_SESSION_OBSERVER_LOCK_STALE_MINUTES", "1")

    assert _acquire_codex_checkpoint_lock(config, lock_path) is False
    assert (lock_path / "owner").read_text().startswith("pid=12345\n")

    _release_codex_checkpoint_lock(lock_path)
