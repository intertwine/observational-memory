"""Unit tests for the host-local backup module."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from observational_memory import backup
from observational_memory.config import Config


def _seed_memory(memory_dir, *, reflections="# Reflections\n## Identity\n- Test\n"):
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(reflections)
    (memory_dir / "observations.md").write_text("# Observations\n")
    (memory_dir / "profile.md").write_text("# Profile\n")
    (memory_dir / "active.md").write_text("# Active\n")


def _config(monkeypatch, tmp_path):
    xdg_data = tmp_path / "data"
    xdg_config = tmp_path / "config"
    xdg_data.mkdir(parents=True, exist_ok=True)
    xdg_config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    return Config()


def test_create_snapshot_copies_in_scope_files_only(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    # Out-of-scope artifacts that must never be captured.
    (config.memory_dir / "usage.sqlite").write_bytes(b"\x00binary")
    (config.env_file.parent).mkdir(parents=True, exist_ok=True)
    config.auth_file.write_text("{secret}")

    info = backup.create_snapshot(config, reason="manual")
    assert info is not None
    captured = {p.name for p in info.path.iterdir()}
    assert captured == {
        "reflections.md",
        "observations.md",
        "profile.md",
        "active.md",
        "manifest.json",
    }
    assert "usage.sqlite" not in captured
    assert "auth.json" not in captured


def test_create_snapshot_returns_none_when_no_memory(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    assert backup.create_snapshot(config, reason="manual") is None
    assert not config.backups_dir.exists() or not list(config.backups_dir.glob("manual-*"))


def test_manifest_has_format_version_and_sha256(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    info = backup.create_snapshot(config, reason="manual")
    assert info is not None
    manifest = json.loads((info.path / "manifest.json").read_text())
    assert manifest["format"] == backup.SNAPSHOT_FORMAT
    assert manifest["format_version"] == backup.SNAPSHOT_FORMAT_VERSION
    import hashlib

    for entry in manifest["files"]:
        data = (info.path / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)


def test_snapshot_disabled_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_ENABLED", "0")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    assert backup.create_snapshot(config, reason="manual") is None


def test_apply_retention_count(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "3")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    for _ in range(8):
        backup.create_snapshot(config, reason="manual")
        time.sleep(0.01)
    snapshots = backup.list_snapshots(config)
    assert len(snapshots) == 3
    # Newest survives.
    assert snapshots[0] == snapshots[0]


def test_apply_retention_days(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "0")
    monkeypatch.setenv("OM_BACKUP_RETENTION_DAYS", "7")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    info = backup.create_snapshot(config, reason="manual")
    assert info is not None

    # Synthesize an old snapshot dir by editing the manifest created_at.
    old_dir = config.backups_dir / "manual-20000101T000000Z"
    old_dir.mkdir(parents=True)
    (old_dir / "reflections.md").write_text("old\n")
    old_created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    (old_dir / "manifest.json").write_text(
        json.dumps(
            {
                "format": backup.SNAPSHOT_FORMAT,
                "format_version": 1,
                "snapshot_id": "manual-20000101T000000Z",
                "reason": "manual",
                "created_at": old_created,
                "files": [{"path": "reflections.md", "role": "reflections", "bytes": 4, "sha256": "x"}],
            }
        )
    )
    pruned = backup.apply_retention(config)
    pruned_ids = {p.snapshot_id for p in pruned}
    assert "manual-20000101T000000Z" in pruned_ids
    assert not old_dir.exists()


def test_retention_ignores_manifestless_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "1")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    backup.create_snapshot(config, reason="manual")
    stray = config.backups_dir / "user-copy"
    stray.mkdir(parents=True)
    (stray / "note.txt").write_text("external copy")
    backup.create_snapshot(config, reason="manual")
    assert stray.exists()  # never deleted by retention


def test_temp_dir_not_listed(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    backup.create_snapshot(config, reason="manual")
    leftover = config.backups_dir / ".tmp-deadbeef"
    leftover.mkdir(parents=True)
    listed = {s.snapshot_id for s in backup.list_snapshots(config)}
    assert ".tmp-deadbeef" not in listed


def test_apply_retention_count_keeps_the_newest(monkeypatch, tmp_path):
    # Regression: the original assertion was `snapshots[0] == snapshots[0]` (a
    # tautology). Capture the newest id before pruning and assert it survives,
    # and that survivors are the 3 most-recent by created_at.
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "3")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    created_ids = []
    for i in range(8):
        info = backup.create_snapshot(config, reason="manual")
        assert info is not None
        created_ids.append(info.snapshot_id)
        time.sleep(0.01)
    newest_id = backup.list_snapshots(config)[0].snapshot_id
    assert newest_id == created_ids[-1]
    surviving = [s.snapshot_id for s in backup.list_snapshots(config)]
    assert len(surviving) == 3
    assert newest_id in surviving
    # Survivors must be exactly the 3 most-recently created ids.
    assert set(surviving) == set(created_ids[-3:])


def test_restore_is_transactional_on_midwrite_failure(monkeypatch, tmp_path):
    # Blocker: a mid-restore I/O failure must NOT leave live memory in a mixed
    # state. Inject a failure on the 2nd staged write and assert every live file
    # is rolled back to its pre-restore content.
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir, reflections="# Reflections\n## Identity\n- OLD\n")
    snap = backup.create_snapshot(config, reason="manual")
    assert snap is not None

    # Diverge all live files from the snapshot.
    live = {
        "reflections.md": "# Reflections\n## Identity\n- NEW\n",
        "observations.md": "# Observations NEW\n",
        "profile.md": "# Profile NEW\n",
        "active.md": "# Active NEW\n",
    }
    for name, body in live.items():
        (config.memory_dir / name).write_text(body)

    real_atomic = backup.atomic_write_bytes
    calls = {"n": 0}

    def flaky(path, data, *args, **kwargs):
        # Allow safety-snapshot writes; fail the 2nd restore-stage write.
        if ".restore-" in path.name:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
        return real_atomic(path, data, *args, **kwargs)

    monkeypatch.setattr(backup, "atomic_write_bytes", flaky)

    snap = backup.resolve_snapshot(config, snap.snapshot_id)
    raised = None
    try:
        backup.restore_snapshot(config, snap)
    except backup.RestoreFailedError as exc:
        raised = exc
    assert raised is not None, "expected a RestoreFailedError after rollback"

    # Every live file is back to the pre-restore (NEW) content — no mixed state.
    for name, body in live.items():
        assert (config.memory_dir / name).read_text() == body


def test_restore_subset_snapshot_is_point_in_time(monkeypatch, tmp_path):
    # Major: a snapshot taken when only reflections.md existed must restore to a
    # consistent point-in-time: files added later are removed, and the derived
    # profile/active are regenerated from the restored reflections.
    config = _config(monkeypatch, tmp_path)
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    (config.memory_dir / "reflections.md").write_text("# Reflections\n## Identity\n- OLD\n")
    snap = backup.create_snapshot(config, reason="manual")
    assert snap is not None
    assert set(snap.files) == {"reflections.md"}

    # Now the full set exists with NEW content.
    (config.memory_dir / "reflections.md").write_text("# Reflections\n## Identity\n- NEW\n")
    (config.memory_dir / "observations.md").write_text("# Observations NEW\n")
    (config.memory_dir / "profile.md").write_text("# Profile NEW stale\n")
    (config.memory_dir / "active.md").write_text("# Active NEW stale\n")

    snap = backup.resolve_snapshot(config, snap.snapshot_id)
    backup.restore_snapshot(config, snap)

    # reflections rolled back; observations (not in snapshot) removed.
    assert (config.memory_dir / "reflections.md").read_text() == "# Reflections\n## Identity\n- OLD\n"
    assert not (config.memory_dir / "observations.md").exists()
    # profile/active regenerated from restored reflections (no longer the stale NEW bodies).
    assert config.profile_path.exists()
    assert "NEW stale" not in config.profile_path.read_text()
    assert "NEW stale" not in config.active_path.read_text()


def test_create_snapshot_force_ignores_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_ENABLED", "0")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    assert backup.create_snapshot(config, reason="cluster-init") is None
    forced = backup.create_snapshot(config, reason="cluster-init", force=True)
    assert forced is not None


def test_orphan_temp_dirs_reaped(monkeypatch, tmp_path):
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir)
    backup.create_snapshot(config, reason="manual")
    orphan = config.backups_dir / ".tmp-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "reflections.md").write_text("debris\n")
    # Age it past the reap threshold.
    old = time.time() - backup._ORPHAN_TEMP_AGE_SECONDS - 60
    import os

    os.utime(orphan, (old, old))
    backup.create_snapshot(config, reason="manual")
    assert not orphan.exists()


def test_safe_int_bad_env_does_not_crash_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "not-a-number")
    config = _config(monkeypatch, tmp_path)
    assert config.backup_retention_count == 20  # falls back to default


def test_restore_safety_snapshot_taken_even_when_backups_disabled(monkeypatch, tmp_path):
    # Codex P1: with OM_BACKUP_ENABLED=0, restore must STILL take a pre-restore
    # safety snapshot (force=True), or a mid-restore failure has no rollback.
    monkeypatch.setenv("OM_BACKUP_ENABLED", "0")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir, reflections="# Reflections\n## Identity\n- GOOD\n")
    good = backup.create_snapshot(config, reason="manual", force=True)
    assert good is not None

    config.reflections_path.write_text("CORRUPT\n")
    safety = backup.restore_snapshot(config, backup.resolve_snapshot(config, good.snapshot_id))

    assert safety.reason == "pre-restore"
    assert safety.path.exists()
    pre_restore = [s for s in backup.list_snapshots(config) if s.reason == "pre-restore"]
    assert pre_restore, "restore must create a pre-restore safety snapshot even when disabled"
    assert config.reflections_path.read_text() == "# Reflections\n## Identity\n- GOOD\n"


def test_restore_does_not_prune_its_source_snapshot(monkeypatch, tmp_path):
    # Codex P2: with a tight retention count, the pre-restore snapshot's own
    # retention pass must not delete the snapshot being restored FROM.
    monkeypatch.setenv("OM_BACKUP_RETENTION_COUNT", "1")
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir, reflections="# Reflections\n## Identity\n- GOOD\n")
    good = backup.create_snapshot(config, reason="manual")
    assert good is not None

    config.reflections_path.write_text("CORRUPT\n")
    backup.restore_snapshot(config, backup.resolve_snapshot(config, good.snapshot_id))

    surviving = {s.snapshot_id for s in backup.list_snapshots(config)}
    assert good.snapshot_id in surviving, "the snapshot being restored must survive its own pre-restore retention"
    assert config.reflections_path.read_text() == "# Reflections\n## Identity\n- GOOD\n"


def _tamper_manifest(snapshot_path, mutate):
    manifest = json.loads((snapshot_path / "manifest.json").read_text())
    mutate(manifest)
    (snapshot_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: m["files"][0].update(sha256=""), id="empty-sha256"),
        pytest.param(lambda m: m["files"][0].pop("sha256", None), id="missing-sha256"),
        pytest.param(lambda m: m["files"][0].update(sha256="z" * 64), id="malformed-sha256"),
        pytest.param(lambda m: m["files"][0].pop("bytes", None), id="missing-bytes"),
        pytest.param(lambda m: m["files"][0].update(path="../evil.md"), id="path-traversal-name"),
        pytest.param(lambda m: m.update(format_version=999), id="unknown-format-version"),
        pytest.param(
            lambda m: m["files"].append(dict(m["files"][0])),
            id="duplicate-role",
        ),
    ],
)
def test_restore_fails_closed_on_untrustworthy_manifest(monkeypatch, tmp_path, mutate):
    # Codex P2 (integrity): a manifest we cannot fully trust must abort BEFORE
    # writing anything — live memory stays untouched and no safety snapshot is
    # taken (validation precedes Phase 2).
    config = _config(monkeypatch, tmp_path)
    _seed_memory(config.memory_dir, reflections="# Reflections\n## Identity\n- GOOD\n")
    good = backup.create_snapshot(config, reason="manual")
    assert good is not None
    _tamper_manifest(good.path, mutate)

    # Diverge live memory so we can prove it is NOT overwritten.
    config.reflections_path.write_text("LIVE-UNTOUCHED\n")

    with pytest.raises((ValueError, FileNotFoundError)):
        backup.restore_snapshot(config, backup.resolve_snapshot(config, good.snapshot_id))

    assert config.reflections_path.read_text() == "LIVE-UNTOUCHED\n"
    assert not [s for s in backup.list_snapshots(config) if s.reason == "pre-restore"]
