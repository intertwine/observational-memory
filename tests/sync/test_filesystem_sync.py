import base64
import json

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    create_invite_token,
    initialize_cluster_config,
    join_cluster_from_invite,
)
from observational_memory.sync.crypto import wrap_key_for_node
from observational_memory.sync.engine import sync_cluster
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.store import ClusterStore, NodeMetadata, new_data_key_b64
from observational_memory.sync.transports.filesystem import FilesystemTransport


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


def _node_config(tmp_path, name):
    return Config(memory_dir=tmp_path / name / "memory", env_file=tmp_path / name / "config" / "env")


def test_two_nodes_converge_over_filesystem_transport(tmp_path):
    shared = tmp_path / "shared"
    config_a = _node_config(tmp_path, "a")
    cluster_a = initialize_cluster_config(
        config_a,
        name="Cluster",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(shared))],
    )
    store_a = ClusterStore.from_config(config_a)
    store_a.ensure_layout()
    _membership(store_a)
    invite_token = create_invite_token(config_a, cluster_a, expires="1h", mode="trusted-direct")

    config_b = _node_config(tmp_path, "b")
    _cluster_b, invite = join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    _membership(store_b, invite=invite)

    store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-a"},
        payload={"format": "markdown", "body": "- memory from A", "observed_at": "2026-05-08T12:00:00Z"},
    )
    store_b.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-b"},
        payload={"format": "markdown", "body": "- memory from B", "observed_at": "2026-05-08T12:01:00Z"},
    )

    sync_cluster(config_a)
    sync_cluster(config_b)
    sync_cluster(config_a)
    sync_cluster(config_b)
    materialize_cluster_memory(config_a, ClusterStore.from_config(config_a))
    materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))

    a_text = config_a.observations_path.read_text()
    b_text = config_b.observations_path.read_text()
    assert "memory from A" in a_text
    assert "memory from B" in a_text
    assert "memory from A" in b_text
    assert "memory from B" in b_text
    shared_records = b"".join(path.read_bytes() for path in shared.glob("clusters/*/records/*/*.omr.json"))
    assert b"memory from A" not in shared_records
    assert not list(shared.glob("**/node.json"))
    assert not list(shared.glob("**/cluster.key"))


def test_key_rotation_propagates_active_key_to_peer(tmp_path):
    shared = tmp_path / "shared"
    config_a = _node_config(tmp_path, "a")
    cluster_a = initialize_cluster_config(
        config_a,
        name="Cluster",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(shared))],
    )
    store_a = ClusterStore.from_config(config_a)
    store_a.ensure_layout()
    _membership(store_a)
    invite_token = create_invite_token(config_a, cluster_a, expires="1h", mode="trusted-direct")

    config_b = _node_config(tmp_path, "b")
    _cluster_b, invite = join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    _membership(store_b, invite=invite)

    sync_cluster(config_b)
    sync_cluster(config_a)
    sync_cluster(config_b)
    sync_cluster(config_a)
    store_a = ClusterStore.from_config(config_a)
    store_b = ClusterStore.from_config(config_b)
    assert store_b.cluster_config.node_id in store_a.public_nodes()
    data_key_b64 = new_data_key_b64()
    key_id = f"key_{store_a.cluster_config.node_id}_test"
    rotation = store_a.append_record(
        kind="key_epoch",
        namespace="personal",
        source={"agent": "test"},
        payload={
            "epoch": len(store_a.secret.data_keys) + 1,
            "key_id": key_id,
            "recipients": [
                {
                    "node_id": node.node_id,
                    "wrapped_key": wrap_key_for_node(
                        data_key_b64,
                        node.encryption_public_key_b64,
                        aad=f"{store_a.cluster_config.id}:{key_id}".encode("utf-8"),
                    ),
                }
                for node in store_a.public_nodes().values()
                if node.encryption_public_key_b64
            ],
            "excluded_nodes": [],
            "created_at": "2026-05-08T12:00:00Z",
        },
    )
    sync_cluster(config_a)
    sync_cluster(config_b)

    store_b = ClusterStore.from_config(config_b)
    assert store_b.secret.active_key_id == key_id
    assert store_b.secret.active_key_hlc == rotation.hlc

    observation = store_b.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-b"},
        payload={"format": "markdown", "body": "- post-rotation", "observed_at": "2026-05-08T12:01:00Z"},
    )
    assert observation.data["encryption"]["key_id"] == store_b.secret.active_key_id


def test_key_epoch_excludes_revoked_peer_from_new_active_key(tmp_path):
    shared = tmp_path / "shared"
    config_a = _node_config(tmp_path, "a")
    cluster_a = initialize_cluster_config(
        config_a,
        name="Cluster",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(shared))],
    )
    store_a = ClusterStore.from_config(config_a)
    store_a.ensure_layout()
    _membership(store_a)
    invite_token = create_invite_token(config_a, cluster_a, expires="1h", mode="trusted-direct")

    config_b = _node_config(tmp_path, "b")
    _cluster_b, invite = join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    _membership(store_b, invite=invite)

    sync_cluster(config_b)
    sync_cluster(config_a)
    sync_cluster(config_b)
    sync_cluster(config_a)
    store_a = ClusterStore.from_config(config_a)
    store_b = ClusterStore.from_config(config_b)
    assert store_b.cluster_config.node_id in store_a.public_nodes()
    old_key_id = store_b.secret.active_key_id
    revoked_node_id = store_b.cluster_config.node_id
    revoke_record = store_a.append_record(
        kind="node_membership",
        namespace="personal",
        source={"agent": "test"},
        payload={
            "operation": "revoke",
            "node_id": revoked_node_id,
            "created_at": "2026-05-08T12:00:00Z",
        },
    )

    sync_cluster(config_a)
    sync_cluster(config_b)
    store_b = ClusterStore.from_config(config_b)
    assert store_b.public_nodes()[revoked_node_id].revoked is True
    assert store_b.public_nodes()[revoked_node_id].revoked_after_hlc == revoke_record.hlc

    store_a = ClusterStore.from_config(config_a)
    data_key_b64 = new_data_key_b64()
    key_id = f"key_{store_a.cluster_config.node_id}_post_revoke"
    rotation = store_a.append_record(
        kind="key_epoch",
        namespace="personal",
        source={"agent": "test"},
        payload={
            "epoch": len(store_a.secret.data_keys) + 1,
            "key_id": key_id,
            "recipients": [
                {
                    "node_id": node.node_id,
                    "wrapped_key": wrap_key_for_node(
                        data_key_b64,
                        node.encryption_public_key_b64,
                        aad=f"{store_a.cluster_config.id}:{key_id}".encode("utf-8"),
                    ),
                }
                for node in store_a.public_nodes().values()
                if not node.revoked and node.encryption_public_key_b64
            ],
            "excluded_nodes": [revoked_node_id],
            "created_at": "2026-05-08T12:01:00Z",
        },
    )
    assert store_a.secret.active_key_id == key_id

    sync_cluster(config_a)
    summary = sync_cluster(config_b)
    store_b = ClusterStore.from_config(config_b)
    assert summary.rejected >= 1
    assert store_b.secret.active_key_id == old_key_id
    assert store_b.secret.active_key_hlc != rotation.hlc


def test_filesystem_transport_rejects_filename_body_mismatches(tmp_path):
    shared = tmp_path / "shared"
    config_a = _node_config(tmp_path, "a")
    cluster_a = initialize_cluster_config(
        config_a,
        name="Cluster",
        node_alias="node-a",
        transports=[TransportConfig(type="filesystem", path=str(shared))],
    )
    store_a = ClusterStore.from_config(config_a)
    store_a.ensure_layout()
    _membership(store_a)
    record = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- memory", "observed_at": "2026-05-08T12:00:00Z"},
    )
    transport = FilesystemTransport(shared)
    node_dir = shared / "clusters" / cluster_a.id / "nodes"
    node_dir.mkdir(parents=True)
    (node_dir / "node_attacker.json").write_text(
        json.dumps(
            {
                "version": 1,
                "node_id": store_a.cluster_config.node_id,
                "alias": "mismatch",
                "signing_public_key_b64": "abc",
            }
        )
    )
    record_dir = shared / "clusters" / cluster_a.id / "records" / "node_attacker"
    record_dir.mkdir(parents=True)
    (record_dir / f"{record.node_seq:020d}-{record.record_id}.omr.json").write_bytes(record.to_bytes())

    assert transport.fetch_node(cluster_a.id, "node_attacker") is None
    assert transport.fetch_record(cluster_a.id, "node_attacker", record.record_id) is None


def test_cli_init_status_materialize(isolated_om_home):
    runner = CliRunner()
    shared = isolated_om_home / "shared"

    result = runner.invoke(
        cli,
        [
            "cluster",
            "init",
            "--name",
            "CLI Cluster",
            "--node-alias",
            "cli-node",
            "--transport",
            f"filesystem:{shared}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Initialized OM Cluster" in result.output

    result = runner.invoke(cli, ["cluster", "status", "--json"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["initialized"] is True
    assert status["node"]["alias"] == "cli-node"
    assert status["pending_peers"] == {}

    store = ClusterStore.from_config(Config())
    pending = NodeMetadata(
        node_id="node_pending",
        alias="pending",
        signing_public_key_b64="abc",
    )
    assert store.import_node_metadata_bytes(json.dumps(pending.to_dict()).encode("utf-8")) is True

    result = runner.invoke(cli, ["cluster", "status", "--json"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["pending_peers"]["node_pending"]["alias"] == "pending"

    result = runner.invoke(cli, ["cluster", "status"])
    assert result.exit_code == 0, result.output
    assert "Pending peers:" in result.output
    assert "node_pending pending" in result.output

    result = runner.invoke(cli, ["cluster", "materialize", "--no-reindex"])
    assert result.exit_code == 0, result.output


def test_cli_namespace_source_policy_and_override_semantics(isolated_om_home):
    runner = CliRunner()
    shared = isolated_om_home / "shared"
    result = runner.invoke(
        cli,
        [
            "cluster",
            "init",
            "--name",
            "CLI Cluster",
            "--node-alias",
            "cli-node",
            "--transport",
            f"filesystem:{shared}",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["cluster", "namespace", "add", "project:om"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli,
        ["cluster", "source-policy", "add", "--agent", "codex", "--namespace", "project:om", "--local-only"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["cluster", "source-policy", "list", "--json"])
    assert result.exit_code == 0, result.output
    policies = json.loads(result.stdout)
    policy = next(item for item in policies if item["source"] == "codex")
    assert policy["local_only"] is True

    result = runner.invoke(
        cli,
        ["cluster", "override", "set", "--target", "profile", "--section", "communication", "--body", "First."],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli,
        ["cluster", "override", "set", "--target", "profile", "--section", "communication", "--body", "Second."],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["cluster", "override", "get", "--target", "profile", "--section", "communication"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "Second."
    result = runner.invoke(cli, ["cluster", "override", "remove", "--target", "profile", "--section", "communication"])
    assert result.exit_code == 0, result.output


def test_cli_invite_join_revoke_rotate(tmp_path):
    runner = CliRunner()
    shared = tmp_path / "shared"

    env_a = {
        "HOME": str(tmp_path / "a-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "a-config"),
        "XDG_DATA_HOME": str(tmp_path / "a-data"),
        "CODEX_HOME": str(tmp_path / "a-codex"),
    }
    env_b = {
        "HOME": str(tmp_path / "b-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "b-config"),
        "XDG_DATA_HOME": str(tmp_path / "b-data"),
        "CODEX_HOME": str(tmp_path / "b-codex"),
    }

    result = runner.invoke(
        cli,
        ["cluster", "init", "--name", "CLI Cluster", "--node-alias", "node-a", "--transport", f"filesystem:{shared}"],
        env=env_a,
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["cluster", "invite", "--mode", "trusted-direct", "--expires", "1h"], env=env_a)
    assert result.exit_code == 0, result.output
    assert "carries cluster key material" in result.stderr
    token = result.stdout.strip()
    assert token.startswith("omc1:")

    result = runner.invoke(cli, ["cluster", "join", token, "--node-alias", "node-b"], env=env_b)
    assert result.exit_code == 0, result.output
    assert "Joined OM Cluster" in result.output

    result = runner.invoke(cli, ["cluster", "sync", "--json"], env=env_b)
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["cluster", "revoke", "node_fake"], env=env_a)
    assert result.exit_code == 0, result.output
    assert "Revoked node_fake" in result.output

    result = runner.invoke(cli, ["cluster", "rotate-key"], env=env_a)
    assert result.exit_code == 0, result.output
    assert "Rotated cluster data key" in result.output


def test_cli_request_invite_approval_flow(tmp_path):
    runner = CliRunner()
    shared = tmp_path / "shared"

    env_a = {
        "HOME": str(tmp_path / "a-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "a-config"),
        "XDG_DATA_HOME": str(tmp_path / "a-data"),
        "CODEX_HOME": str(tmp_path / "a-codex"),
    }
    env_b = {
        "HOME": str(tmp_path / "b-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "b-config"),
        "XDG_DATA_HOME": str(tmp_path / "b-data"),
        "CODEX_HOME": str(tmp_path / "b-codex"),
    }

    result = runner.invoke(
        cli,
        ["cluster", "init", "--name", "CLI Cluster", "--node-alias", "node-a", "--transport", f"filesystem:{shared}"],
        env=env_a,
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(cli, ["cluster", "invite", "--expires", "1h"], env=env_a)
    assert result.exit_code == 0, result.output
    assert "does not carry cluster data keys" in result.stderr
    token = result.stdout.strip()
    token_body = token.split(":", 1)[1]
    decoded = json.loads(base64.urlsafe_b64decode(token_body + "=" * (-len(token_body) % 4)))
    assert "data_keys" not in decoded["body"]

    result = runner.invoke(cli, ["cluster", "join", token, "--node-alias", "node-b"], env=env_b)
    assert result.exit_code == 0, result.output
    assert "Created pending OM Cluster join request" in result.output
    request_id = [part for part in result.output.split() if part.startswith("join_")][0]

    result = runner.invoke(cli, ["cluster", "sync"], env=env_b)
    assert result.exit_code != 0
    assert not list(shared.glob("clusters/*/records/node_*/*.omr.json"))

    result = runner.invoke(cli, ["cluster", "requests", "--json"], env=env_a)
    assert result.exit_code == 0, result.output
    requests = json.loads(result.stdout)
    assert requests[0]["request_id"] == request_id
    assert requests[0]["status"] == "pending"

    result = runner.invoke(cli, ["cluster", "approve", request_id], env=env_a)
    assert result.exit_code == 0, result.output
    assert "Approved" in result.output
    shared_bytes = b"".join(path.read_bytes() for path in shared.glob("clusters/**/*") if path.is_file())
    assert b"request_secret_b64" not in shared_bytes
    assert b"data_keys" not in shared_bytes

    result = runner.invoke(cli, ["cluster", "sync", "--json"], env=env_b)
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["pulled"] >= 1

    result = runner.invoke(cli, ["cluster", "status", "--json"], env=env_b)
    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["enabled"] is True
    assert status["join_request"]["status"] == "approved"
    assert any(peer["alias"] == "node-b" for peer in status["peers"].values())
