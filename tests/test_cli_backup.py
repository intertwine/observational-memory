"""Tests for `om backup` and `om restore` commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from observational_memory.cli import cli


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
    monkeypatch.delenv("OM_CLUSTER_ENABLED", raising=False)


def _memory_dir(tmp_path):
    return tmp_path / "data" / "observational-memory"


def _seed(tmp_path, *, reflections="# Reflections\n## Identity\n- Bryan\n"):
    memory_dir = _memory_dir(tmp_path)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(reflections)
    (memory_dir / "observations.md").write_text("# Observations\n")
    (memory_dir / "profile.md").write_text("# Profile\n")
    (memory_dir / "active.md").write_text("# Active\n")
    return memory_dir


def test_backup_creates_manual_snapshot(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _seed(tmp_path)
    result = CliRunner().invoke(cli, ["backup"])
    assert result.exit_code == 0, result.output
    assert "Snapshot created" in result.output
    backups = list((_memory_dir(tmp_path) / "backups").glob("manual-*"))
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").exists()


def test_backup_list_json(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _seed(tmp_path)
    CliRunner().invoke(cli, ["backup"])
    result = CliRunner().invoke(cli, ["backup", "--list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data and data[0]["reason"] == "manual"


def test_restore_round_trip_byte_faithful(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    original = {
        name: (memory_dir / name).read_bytes()
        for name in ("reflections.md", "observations.md", "profile.md", "active.md")
    }
    backup_result = CliRunner().invoke(cli, ["backup", "--json"])
    assert backup_result.exit_code == 0, backup_result.output
    snapshot_id = json.loads(backup_result.output)["snapshot_id"]

    # Corrupt live memory.
    (memory_dir / "reflections.md").write_text("GARBAGE\n")

    result = CliRunner().invoke(cli, ["restore", snapshot_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output
    for name, content in original.items():
        assert (memory_dir / name).read_bytes() == content


def test_restore_latest(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    CliRunner().invoke(cli, ["backup"])
    (memory_dir / "reflections.md").write_text("changed\n")
    result = CliRunner().invoke(cli, ["restore", "--latest", "--yes"])
    assert result.exit_code == 0, result.output
    assert (memory_dir / "reflections.md").read_text() == "# Reflections\n## Identity\n- Bryan\n"


def test_restore_requires_selection_without_latest(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    CliRunner().invoke(cli, ["backup"])
    (memory_dir / "reflections.md").write_text("changed\n")
    result = CliRunner().invoke(cli, ["restore"])
    assert result.exit_code == 0, result.output
    assert "Choose a snapshot" in result.output
    # Did not overwrite.
    assert (memory_dir / "reflections.md").read_text() == "changed\n"


def test_restore_confirmation_required(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    bk = CliRunner().invoke(cli, ["backup", "--json"])
    snapshot_id = json.loads(bk.output)["snapshot_id"]
    (memory_dir / "reflections.md").write_text("changed\n")
    result = CliRunner().invoke(cli, ["restore", snapshot_id], input="n\n")
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert (memory_dir / "reflections.md").read_text() == "changed\n"


def test_restore_unknown_id_fails_cleanly(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    CliRunner().invoke(cli, ["backup"])
    result = CliRunner().invoke(cli, ["restore", "bogus-id", "--yes"])
    assert result.exit_code != 0
    assert "No snapshot named" in result.output
    assert "Traceback" not in result.output
    # Memory intact.
    assert (memory_dir / "reflections.md").read_text() == "# Reflections\n## Identity\n- Bryan\n"


def test_restore_round_trip_byte_faithful_edge_bytes(monkeypatch, tmp_path):
    # Headline 'byte-faithful' claim: no trailing newline, CRLF, and non-UTF-8 bytes.
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    (memory_dir / "reflections.md").write_bytes(b"# R\r\nno trailing newline")
    (memory_dir / "observations.md").write_bytes(b"\xff\xfe binary \x00 bytes")
    original = {
        name: (memory_dir / name).read_bytes()
        for name in ("reflections.md", "observations.md", "profile.md", "active.md")
    }
    bk = CliRunner().invoke(cli, ["backup", "--json"])
    snapshot_id = json.loads(bk.output)["snapshot_id"]
    (memory_dir / "reflections.md").write_text("clobbered\n")
    result = CliRunner().invoke(cli, ["restore", snapshot_id, "--yes"])
    assert result.exit_code == 0, result.output
    for name, content in original.items():
        assert (memory_dir / name).read_bytes() == content


def test_restore_no_safety_snapshot(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path)
    bk = CliRunner().invoke(cli, ["backup", "--json"])
    snapshot_id = json.loads(bk.output)["snapshot_id"]
    (memory_dir / "reflections.md").write_text("changed\n")
    result = CliRunner().invoke(cli, ["restore", snapshot_id, "--yes", "--no-safety-snapshot", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["safety_snapshot"] is None
    # No pre-restore snapshot directory was created.
    backups = list((_memory_dir(tmp_path) / "backups").glob("pre-restore-*"))
    assert backups == []


def test_restore_rejects_id_and_latest_together(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    _seed(tmp_path)
    bk = CliRunner().invoke(cli, ["backup", "--json"])
    snapshot_id = json.loads(bk.output)["snapshot_id"]
    result = CliRunner().invoke(cli, ["restore", snapshot_id, "--latest", "--yes"])
    assert result.exit_code != 0
    assert "not both" in result.output
    assert "Traceback" not in result.output


def test_restore_midwrite_failure_surfaces_clean_error(monkeypatch, tmp_path):
    # Major (CLI): a mid-restore OSError must surface as a one-line CLI error
    # naming recovery, not a raw traceback, and the auto-rollback restores memory.
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = _seed(tmp_path, reflections="# Reflections\n## Identity\n- OLD\n")
    bk = CliRunner().invoke(cli, ["backup", "--json"])
    snapshot_id = json.loads(bk.output)["snapshot_id"]
    (memory_dir / "reflections.md").write_text("NEW\n")

    from observational_memory import backup as backup_mod

    real_atomic = backup_mod.atomic_write_bytes
    calls = {"n": 0}

    def flaky(path, data, *args, **kwargs):
        if ".restore-" in path.name:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
        return real_atomic(path, data, *args, **kwargs)

    monkeypatch.setattr(backup_mod, "atomic_write_bytes", flaky)
    result = CliRunner().invoke(cli, ["restore", snapshot_id, "--yes"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "rolled back" in result.output
    # Rolled back to pre-restore content.
    assert (memory_dir / "reflections.md").read_text() == "NEW\n"
