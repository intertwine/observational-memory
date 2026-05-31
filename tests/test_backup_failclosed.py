"""Fail-closed and integrity tests for the backup module."""

from __future__ import annotations

from observational_memory import backup
from observational_memory.config import Config


def _config(monkeypatch, tmp_path):
    xdg_data = tmp_path / "data"
    xdg_config = tmp_path / "config"
    xdg_data.mkdir(parents=True, exist_ok=True)
    xdg_config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    return Config()


def _seed_memory(memory_dir):
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text("# Reflections\n## Identity\n- Test\n")
    (memory_dir / "observations.md").write_text("# Observations\n")
    (memory_dir / "profile.md").write_text("# Profile\n")
    (memory_dir / "active.md").write_text("# Active\n")


def test_failclosed_swallows_ioerror(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "create_snapshot", boom)
    assert backup.create_snapshot_failclosed(config, reason="pre-reflect") is None


def test_restore_aborts_on_hash_mismatch(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    info = backup.create_snapshot(config, reason="manual")
    assert info is not None

    # Corrupt a snapshot file after the manifest was written.
    (info.path / "reflections.md").write_text("CORRUPTED\n")

    live_before = config.reflections_path.read_text()
    snapshot = backup.resolve_snapshot(config, info.snapshot_id)
    try:
        backup.restore_snapshot(config, snapshot, make_safety_snapshot=False)
        raised = False
    except ValueError:
        raised = True
    assert raised
    # Live memory untouched.
    assert config.reflections_path.read_text() == live_before


def test_restore_takes_pre_restore_safety_snapshot(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    first = backup.create_snapshot(config, reason="manual")
    assert first is not None

    # Mutate live memory, then restore the first snapshot.
    config.reflections_path.write_text("# Reflections\n## Identity\n- Mutated\n")
    mutated = config.reflections_path.read_text()

    snapshot = backup.resolve_snapshot(config, first.snapshot_id)
    safety = backup.restore_snapshot(config, snapshot)
    assert safety.reason == "pre-restore"
    # The safety snapshot captured the mutated (pre-restore) live content.
    assert (safety.path / "reflections.md").read_text() == mutated
    # Live memory now matches the restored snapshot.
    assert config.reflections_path.read_text() == (snapshot.path / "reflections.md").read_text()
