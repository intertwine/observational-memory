"""Guard: the host-local usage DB must never be materialized or synced via OM Cluster.

Project rule (CLAUDE.md): usage tracking stays host-local and is never synced.
"""

from __future__ import annotations

from datetime import datetime, timezone

from observational_memory.config import Config
from observational_memory.sync.config import TransportConfig, initialize_cluster_config
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.store import ClusterStore
from observational_memory.usage import SYNC_EXCLUDED


def test_usage_db_name_is_in_sync_excluded():
    assert "usage.sqlite" in SYNC_EXCLUDED


def test_default_usage_db_lives_in_memory_dir_but_is_excluded(tmp_path):
    cfg = Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")
    assert cfg.usage_db_path.parent == cfg.memory_dir
    assert cfg.usage_db_path.name in SYNC_EXCLUDED


def _init_store(tmp_path):
    config = Config(
        memory_dir=tmp_path / "node-a" / "memory",
        env_file=tmp_path / "node-a" / "config" / "env",
        observation_retention_days=36500,
    )
    cluster_config = initialize_cluster_config(
        config,
        name="Guard",
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
    return config, store


def test_materialize_does_not_touch_usage_db(tmp_path):
    config, store = _init_store(tmp_path)
    config.ensure_memory_dir()

    # Plant a usage DB next to the memory artifacts with sentinel content.
    usage_db = config.usage_db_path
    usage_db.write_bytes(b"SENTINEL-USAGE-DB")

    store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={
            "format": "markdown",
            "body": "# Observations\n\n- hello",
            "observed_at": "2026-05-08T12:00:00Z",
        },
    )
    materialize_cluster_memory(config, store)

    # The materialized view writes observations.md but never reads/writes usage.sqlite.
    assert config.observations_path.exists()
    assert usage_db.read_bytes() == b"SENTINEL-USAGE-DB"

    # No cluster record should have been derived from the usage DB.
    kinds = [r.kind for r in store.list_records(include_tombstoned=True)]
    assert all(k in {"node_membership", "observation"} for k in kinds)
