import json
from datetime import datetime, timezone

from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    create_invite_token,
    initialize_cluster_config,
    join_cluster_from_invite,
)
from observational_memory.sync.materialize import choose_reflection_snapshot, materialize_cluster_memory
from observational_memory.sync.store import ClusterStore, NodeMetadata


def _init_store(tmp_path, name="Test", alias="node-a"):
    config = Config(
        memory_dir=tmp_path / alias / "memory",
        env_file=tmp_path / alias / "config" / "env",
        observation_retention_days=36500,
    )
    cluster_config = initialize_cluster_config(
        config,
        name=name,
        node_alias=alias,
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


def test_append_read_heads_permissions_and_no_plaintext(tmp_path):
    config, store = _init_store(tmp_path)

    record = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-a"},
        payload={"format": "markdown", "body": "- top secret", "observed_at": "2026-05-08T12:00:00Z"},
    )

    assert store.read_payload(record)["body"] == "- top secret"
    assert store.all_heads()[store.cluster_config.node_id] == 2
    record_file = store._record_path_by_id(record.record_id)
    assert record_file is not None
    assert b"top secret" not in record_file.read_bytes()
    assert oct((config.cluster_keys_dir / store.cluster_config.id / "node.json").stat().st_mode & 0o777) == "0o600"
    assert oct((config.cluster_keys_dir / store.cluster_config.id / "cluster.key").stat().st_mode & 0o777) == "0o600"
    assert record.record_id in json.loads(store.record_index_path.read_text())["records"]
    assert store._record_path_by_id(record.record_id) == record_file


def test_record_index_is_rebuildable(tmp_path):
    _config, store = _init_store(tmp_path)
    record = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- indexed", "observed_at": "2026-05-08T12:00:00Z"},
    )
    store.record_index_path.unlink()

    index = store.rebuild_record_index()

    assert record.record_id in index["records"]
    assert store._record_path_by_id(record.record_id).exists()


def test_duplicate_import_is_idempotent_and_tamper_rejected(tmp_path):
    config_a, store_a = _init_store(tmp_path, alias="node-a")
    invite_token = create_invite_token(config_a, store_a.cluster_config, expires="1h", mode="trusted-direct")
    config_b = Config(memory_dir=tmp_path / "node-b" / "memory", env_file=tmp_path / "node-b" / "config" / "env")
    join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    record = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- shared", "observed_at": "2026-05-08T12:00:00Z"},
    )

    first = store_b.import_record_bytes(record.to_bytes())
    second = store_b.import_record_bytes(record.to_bytes())
    assert first.imported
    assert second.status == "duplicate"

    tampered = json.loads(record.to_bytes())
    tampered["source"]["agent"] = "evil"
    rejected = store_b.import_record_bytes(json.dumps(tampered).encode("utf-8"))
    assert rejected.status == "rejected"


def test_public_node_metadata_import_is_pending_only(tmp_path):
    _config, store = _init_store(tmp_path)
    metadata = NodeMetadata(
        node_id="node_pending",
        alias="pending",
        signing_public_key_b64="abc",
    )

    assert store.import_node_metadata_bytes(json.dumps(metadata.to_dict()).encode("utf-8")) is True
    assert "node_pending" not in store.public_nodes()
    assert (store.pending_nodes_dir / "node_pending.json").exists()
    assert store.import_node_metadata_bytes(json.dumps(metadata.to_dict()).encode("utf-8")) is False


def test_public_node_metadata_import_is_capped_and_path_safe(tmp_path, monkeypatch):
    _config, store = _init_store(tmp_path)
    monkeypatch.setattr("observational_memory.sync.store._MAX_PENDING_NODE_METADATA", 1)
    first = NodeMetadata(
        node_id="node_first",
        alias="first",
        signing_public_key_b64="abc",
    )
    second = NodeMetadata(
        node_id="node_second",
        alias="second",
        signing_public_key_b64="def",
    )
    unsafe = NodeMetadata(
        node_id="../escape",
        alias="escape",
        signing_public_key_b64="ghi",
    )

    assert store.import_node_metadata_bytes(json.dumps(first.to_dict()).encode("utf-8")) is True
    assert store.import_node_metadata_bytes(json.dumps(second.to_dict()).encode("utf-8")) is False
    assert store.import_node_metadata_bytes(json.dumps(unsafe.to_dict()).encode("utf-8")) is False
    assert set(store.pending_nodes()) == {"node_first"}
    assert not (store.pending_nodes_dir.parent / "escape.json").exists()


def test_malformed_record_ids_are_rejected_before_path_use(tmp_path):
    _config, store = _init_store(tmp_path)
    record = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- safe", "observed_at": "2026-05-08T12:00:00Z"},
    )
    raw = json.loads(record.to_bytes())
    raw["record_id"] = "../escape"

    result = store.import_record_bytes(json.dumps(raw).encode("utf-8"))

    assert result.status == "rejected"
    assert "Invalid record_id" in (result.reason or "")
    assert not (store.records_dir.parent / "escape.omr.json").exists()


def test_out_of_order_import_does_not_regress_head_record_id(tmp_path):
    config_a, store_a = _init_store(tmp_path, alias="node-a")
    invite_token = create_invite_token(config_a, store_a.cluster_config, expires="1h", mode="trusted-direct")
    config_b = Config(memory_dir=tmp_path / "node-b" / "memory", env_file=tmp_path / "node-b" / "config" / "env")
    join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    seq2 = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- second", "observed_at": "2026-05-08T12:00:00Z"},
    )
    seq3 = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- third", "observed_at": "2026-05-08T12:01:00Z"},
    )

    assert store_b.import_record_bytes(seq3.to_bytes()).imported
    assert store_b.import_record_bytes(seq2.to_bytes()).imported

    head = store_b._read_head(store_a.cluster_config.node_id)
    assert head is not None
    assert head["seq"] == seq3.node_seq
    assert head["record_id"] == seq3.record_id


def test_revoked_node_future_records_are_rejected(tmp_path):
    config_a, store_a = _init_store(tmp_path, alias="node-a")
    invite_token = create_invite_token(config_a, store_a.cluster_config, expires="1h", mode="trusted-direct")
    config_b = Config(memory_dir=tmp_path / "node-b" / "memory", env_file=tmp_path / "node-b" / "config" / "env")
    join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    node_a = store_a.public_nodes()[store_a.cluster_config.node_id]
    store_b.write_node_metadata(
        NodeMetadata(
            node_id=node_a.node_id,
            alias=node_a.alias,
            signing_public_key_b64=node_a.signing_public_key_b64,
            revoked=True,
            revoked_after_hlc="2000-01-01T00:00:00.000000Z-000000-node_a",
        )
    )
    record = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- rejected", "observed_at": "2026-05-08T12:00:00Z"},
    )

    result = store_b.import_record_bytes(record.to_bytes())

    assert result.status == "rejected"
    assert "revoked" in (result.reason or "")


def test_materialize_observations_reflections_redactions_and_overrides(tmp_path):
    config, store = _init_store(tmp_path)
    observation = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-a", "project": "om"},
        payload={
            "format": "markdown",
            "body": "# Observations\n\n## 2026-05-08\n\n- hello cluster",
            "observed_at": "2026-05-08T12:00:00Z",
        },
    )
    store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector"},
        payload={
            "format": "markdown",
            "body": (
                "# Reflections\n\n"
                "*Last updated: 2026-05-08 12:00 UTC*\n"
                "*Last reflected: 2026-05-08*\n\n"
                "## Core Identity\n- Test"
            ),
            "frontier": store.records_frontier(),
            "input_record_ids": [observation.record_id],
            "base_snapshot_ids": [],
        },
    )
    override = store.append_record(
        kind="manual_override",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target": "profile", "section": "communication_style", "operation": "upsert", "body": "Be direct."},
    )
    materialize_cluster_memory(config, store)
    assert "hello cluster" in config.observations_path.read_text()
    assert "Generated by Observational Memory Cluster" in config.reflections_path.read_text()
    assert "Be direct." in config.profile_path.read_text()

    store.append_record(
        kind="tombstone",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target_record_id": observation.record_id, "reason": "test"},
    )
    store.append_record(
        kind="tombstone",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target_record_id": override.record_id, "reason": "test"},
    )
    materialize_cluster_memory(config, store)
    assert "hello cluster" not in config.observations_path.read_text()
    assert "Be direct." not in config.profile_path.read_text()


def test_materialize_ignores_backups_dir(tmp_path):
    """Local backups live under memory_dir but must never be touched by sync."""
    config, store = _init_store(tmp_path)
    store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-a"},
        payload={
            "format": "markdown",
            "body": "# Observations\n\n## 2026-05-08\n\n- hello",
            "observed_at": "2026-05-08T12:00:00Z",
        },
    )
    backups_dir = config.backups_dir
    backups_dir.mkdir(parents=True, exist_ok=True)
    sentinel = backups_dir / "manual-20260101T000000Z" / "reflections.md"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("backup sentinel\n")

    materialize_cluster_memory(config, store)

    # The backup is left exactly as-is — never read into or overwritten by sync.
    assert sentinel.read_text() == "backup sentinel\n"
    assert "hello" in config.observations_path.read_text()


def test_manual_overrides_are_latest_wins_by_section(tmp_path):
    config, store = _init_store(tmp_path)
    store.append_record(
        kind="manual_override",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target": "profile", "section": "communication_style", "operation": "upsert", "body": "First."},
    )
    store.append_record(
        kind="manual_override",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target": "profile", "section": "communication_style", "operation": "upsert", "body": "Second."},
    )
    materialize_cluster_memory(config, store)
    assert "Second." in config.profile_path.read_text()
    assert "First." not in config.profile_path.read_text()

    store.append_record(
        kind="manual_override",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target": "profile", "section": "communication_style", "operation": "remove"},
    )
    materialize_cluster_memory(config, store)
    assert "Second." not in config.profile_path.read_text()


def test_tombstoned_reflection_snapshot_is_not_selected(tmp_path):
    _config, store = _init_store(tmp_path)
    older = store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector"},
        payload={
            "format": "markdown",
            "body": "# Reflections\n\n## Core Identity\n- Older",
            "frontier": {"node_a": 1},
            "input_record_ids": [],
            "base_snapshot_ids": [],
        },
    )
    newer = store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector"},
        payload={
            "format": "markdown",
            "body": "# Reflections\n\n## Core Identity\n- Newer",
            "frontier": {"node_a": 2},
            "input_record_ids": [],
            "base_snapshot_ids": [],
        },
    )
    store.append_record(
        kind="tombstone",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target_record_id": newer.record_id, "reason": "test"},
    )

    selected, _catchup = choose_reflection_snapshot(store)

    assert selected is not None
    assert selected.record_id == older.record_id


def test_legacy_import_filters_local_and_unknown_scopes_from_cluster(tmp_path):
    # PR #86 P1: `om cluster init` -> _import_existing_memory writes a shared
    # reflection_snapshot record, so it is a share-OUT path and must route the
    # legacy reflections.md through the same default-deny allowlist. A raw import
    # would sync scope=local AND any explicit-unknown scope off-host as plaintext.
    from observational_memory.cli import _import_existing_memory

    config, store = _init_store(tmp_path)
    config.reflections_path.parent.mkdir(parents=True, exist_ok=True)
    config.reflections_path.write_text(
        "# Reflections\n\n"
        "## Shared\n"
        "- Public fact <!--om: scope=cluster node=node-a-->\n"
        "- Host secret <!--om: scope=local node=node-a-->\n"
        "- Typo scope leaks today <!--om: scope=clustr node=node-a-->\n"
        "- Future tier value <!--om: scope=team node=node-a-->\n"
    )

    _import_existing_memory(store)

    snapshots = store.list_records(kind="reflection_snapshot")
    assert len(snapshots) == 1
    body = store.read_payload(snapshots[0])["body"]
    assert "Public fact" in body  # scope=cluster shared
    assert "Host secret" not in body  # scope=local withheld (pre-existing rule)
    assert "Typo scope leaks today" not in body  # explicit-unknown fails closed
    assert "Future tier value" not in body  # scope=team not yet enabled -> withheld


def test_materialize_writes_conflict_artifact_for_non_snapshot_disagreements(tmp_path):
    config, store = _init_store(tmp_path)
    store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector", "host_alias": "node-a"},
        payload={
            "format": "markdown",
            "body": (
                "# Reflections\n\n"
                "## Preferences & Opinions\n"
                "- Prefers terse reports "
                "<!--om: id=ome_a kind=preference actionability=medium node=node_a scope=cluster-->"
            ),
            "frontier": {"node_a": 1},
            "input_record_ids": [],
            "base_snapshot_ids": [],
        },
    )
    store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector", "host_alias": "node-b"},
        payload={
            "format": "markdown",
            "body": (
                "# Reflections\n\n"
                "## Preferences & Opinions\n"
                "- Prefers detailed reports "
                "<!--om: id=ome_b kind=preference actionability=medium node=node_b scope=cluster-->"
            ),
            "frontier": {"node_b": 1},
            "input_record_ids": [],
            "base_snapshot_ids": [],
        },
    )

    materialize_cluster_memory(config, store)

    conflict_path = store.cluster_dir / "review" / "reflection-conflicts.json"
    assert conflict_path.exists()
    conflicts = json.loads(conflict_path.read_text())
    assert conflicts["count"] == 1
    assert conflicts["conflicts"][0]["kind"] == "preference"


def test_reflection_catchup_uses_observation_frontier_only(tmp_path):
    _config, store = _init_store(tmp_path)
    first = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- first", "observed_at": "2026-05-08T12:00:00Z"},
    )
    second = store.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- second", "observed_at": "2026-05-08T12:01:00Z"},
    )
    store.append_record(
        kind="reflection_snapshot",
        namespace="personal",
        source={"agent": "reflector"},
        payload={
            "format": "markdown",
            "body": "# Reflections\n\n## Core Identity\n- First only",
            "frontier": {store.cluster_config.node_id: first.node_seq},
            "input_record_ids": [first.record_id],
            "base_snapshot_ids": [],
        },
    )
    store.append_record(
        kind="manual_override",
        namespace="personal",
        source={"agent": "manual"},
        payload={"target": "profile", "section": "note", "operation": "upsert", "body": "manual"},
    )

    selected, catchup = choose_reflection_snapshot(store)

    assert selected is not None
    assert second.node_seq > first.node_seq
    assert catchup is True
