"""Tests that reflect takes a fail-closed pre-reflect snapshot."""

from __future__ import annotations

from unittest.mock import patch

from observational_memory import backup
from observational_memory.config import Config
from observational_memory.reflect import run_reflector

_PRIOR_REFLECTIONS = "# Reflections\n\n## Core Identity\n- Name: PRIOR\n"
_NEW_OUTPUT = "# Reflections\n\n## Core Identity\n- Name: NEW\n"


def _config(tmp_path, monkeypatch, strategy):
    monkeypatch.setenv("OM_REFLECTOR_STRATEGY", strategy)
    config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=tmp_path / "projects")
    config.ensure_memory_dir()
    config.reflections_path.write_text(_PRIOR_REFLECTIONS)
    config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test")
    return config


@patch("observational_memory.reflect.compress")
def test_pre_reflect_snapshot_created_legacy(mock_compress, tmp_path, monkeypatch):
    mock_compress.return_value = _NEW_OUTPUT
    config = _config(tmp_path, monkeypatch, "legacy")
    run_reflector(config, dry_run=False)

    snapshots = [s for s in backup.list_snapshots(config) if s.reason == "pre-reflect"]
    assert snapshots, "expected a pre-reflect snapshot"
    # The snapshot captured the PRIOR reflections, not the new output.
    assert (snapshots[0].path / "reflections.md").read_text() == _PRIOR_REFLECTIONS


@patch("observational_memory.reflect.compress")
def test_pre_reflect_snapshot_created_sectioned(mock_compress, tmp_path, monkeypatch):
    mock_compress.return_value = _NEW_OUTPUT
    config = _config(tmp_path, monkeypatch, "sectioned")
    run_reflector(config, dry_run=False)

    snapshots = [s for s in backup.list_snapshots(config) if s.reason == "pre-reflect"]
    assert snapshots, "expected a pre-reflect snapshot for the sectioned strategy"


@patch("observational_memory.reflect.compress")
def test_pre_reflect_snapshot_failure_does_not_break_reflect(mock_compress, tmp_path, monkeypatch):
    mock_compress.return_value = _NEW_OUTPUT
    config = _config(tmp_path, monkeypatch, "legacy")

    def boom(*args, **kwargs):
        raise OSError("snapshot disk full")

    monkeypatch.setattr(backup, "create_snapshot", boom)
    run_reflector(config, dry_run=False)
    # New reflection still written despite the snapshot failure.
    assert "NEW" in config.reflections_path.read_text()


@patch("observational_memory.reflect.compress")
def test_reflect_does_not_snapshot_on_dry_run(mock_compress, tmp_path, monkeypatch):
    mock_compress.return_value = _NEW_OUTPUT
    config = _config(tmp_path, monkeypatch, "legacy")
    run_reflector(config, dry_run=True)
    snapshots = [s for s in backup.list_snapshots(config) if s.reason == "pre-reflect"]
    assert not snapshots


@patch("observational_memory.reflect.compress")
def test_cluster_reflect_takes_pre_reflect_snapshot(mock_compress, tmp_path, monkeypatch):
    # Major: the cluster reflect write path must also take a pre-reflect snapshot
    # (it persists via materialize_cluster_memory, not finalize_reflection).
    from datetime import datetime, timezone

    from observational_memory.config import Config
    from observational_memory.sync.config import TransportConfig, initialize_cluster_config
    from observational_memory.sync.store import ClusterStore

    mock_compress.return_value = _NEW_OUTPUT
    config = Config(
        memory_dir=tmp_path / "memory",
        env_file=tmp_path / "config" / "env",
        observation_retention_days=36500,
    )
    config.ensure_memory_dir()
    config.reflections_path.write_text(_PRIOR_REFLECTIONS)
    config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test")

    cluster_config = initialize_cluster_config(
        config,
        name="Test",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(tmp_path / "shared"))],
    )
    store = ClusterStore.from_config(config)
    store.ensure_layout()
    store.append_record(
        kind="node_membership",
        namespace=cluster_config.default_namespace,
        source={"agent": "test"},
        payload={
            "operation": "add",
            "node_id": cluster_config.node_id,
            "alias": cluster_config.node_alias,
            "signing_public_key": store.keypair.signing_public_key_b64,
            "encryption_public_key": store.keypair.encryption_public_key_b64,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    store.append_record(
        kind="observation",
        namespace=cluster_config.default_namespace,
        source={"agent": "test", "host_alias": "node-a"},
        payload={"format": "markdown", "body": "- new fact", "observed_at": "2026-02-10T14:00:00Z"},
    )

    from observational_memory.reflect import _run_cluster_reflector

    _run_cluster_reflector(config, dry_run=False)
    snapshots = [s for s in backup.list_snapshots(config) if s.reason == "pre-reflect"]
    assert snapshots, "cluster reflect must take a pre-reflect snapshot"
