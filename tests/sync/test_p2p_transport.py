from dataclasses import replace

from test_relay_transport import _relay_server

from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    create_invite_token,
    initialize_cluster_config,
    join_cluster_from_invite,
    write_cluster_config,
)
from observational_memory.sync.engine import sync_cluster
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.store import ClusterStore


def _node_config(tmp_path, name):
    return Config(memory_dir=tmp_path / name / "memory", env_file=tmp_path / name / "config" / "env")


def _membership(store, invite=None):
    payload = {
        "operation": "add",
        "node_id": store.cluster_config.node_id,
        "alias": store.cluster_config.node_alias,
        "signing_public_key": store.keypair.signing_public_key_b64,
        "encryption_public_key": store.keypair.encryption_public_key_b64,
        "created_at": "2026-05-08T12:00:00Z",
    }
    if invite:
        payload["invite"] = invite
    return store.append_record(
        kind="node_membership",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "test"},
        payload=payload,
    )


def test_two_nodes_converge_over_direct_p2p_transport(tmp_path):
    with _relay_server() as peer_a, _relay_server() as peer_b:
        peers = f"{peer_a.url},{peer_b.url}"
        config_a = _node_config(tmp_path, "a")
        cluster_a = initialize_cluster_config(
            config_a,
            name="Cluster",
            node_alias="node-a",
            transports=[TransportConfig(type="p2p", path=peers)],
        )
        store_a = ClusterStore.from_config(config_a)
        store_a.ensure_layout()
        _membership(store_a)
        invite_token = create_invite_token(config_a, cluster_a, expires="1h", mode="trusted-direct")

        config_b = _node_config(tmp_path, "b")
        cluster_b, invite = join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
        write_cluster_config(config_b, replace(cluster_b, transports=[TransportConfig(type="p2p", path=peers)]))
        store_b = ClusterStore.from_config(config_b)
        store_b.ensure_layout()
        _membership(store_b, invite=invite)

        store_a.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "codex", "host_alias": "node-a"},
            payload={"format": "markdown", "body": "- p2p memory from A", "observed_at": "2026-05-08T12:00:00Z"},
        )
        store_b.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "codex", "host_alias": "node-b"},
            payload={"format": "markdown", "body": "- p2p memory from B", "observed_at": "2026-05-08T12:01:00Z"},
        )

        sync_cluster(config_a)
        sync_cluster(config_b)
        sync_cluster(config_a)
        sync_cluster(config_b)
        materialize_cluster_memory(config_a, ClusterStore.from_config(config_a))
        materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))

        assert "p2p memory from A" in config_a.observations_path.read_text()
        assert "p2p memory from B" in config_a.observations_path.read_text()
        assert "p2p memory from A" in config_b.observations_path.read_text()
        assert "p2p memory from B" in config_b.observations_path.read_text()
        peer_bytes = b"".join(peer_a.storage.values()) + b"".join(peer_b.storage.values())
        assert b"p2p memory from A" not in peer_bytes
        assert b"p2p memory from B" not in peer_bytes
