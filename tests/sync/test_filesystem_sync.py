from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    create_invite_token,
    initialize_cluster_config,
    join_cluster_from_invite,
)
from observational_memory.sync.engine import sync_cluster
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.store import ClusterStore


def _membership(store, invite=None):
    payload = {
        "operation": "add",
        "node_id": store.cluster_config.node_id,
        "alias": store.cluster_config.node_alias,
        "signing_public_key": store.keypair.signing_public_key_b64,
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
    invite_token = create_invite_token(config_a, cluster_a, expires="1h")

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
    invite_token = create_invite_token(config_a, cluster_a, expires="1h")

    config_b = _node_config(tmp_path, "b")
    _cluster_b, invite = join_cluster_from_invite(config_b, invite_token, node_alias="node-b")
    store_b = ClusterStore.from_config(config_b)
    store_b.ensure_layout()
    _membership(store_b, invite=invite)

    rotation = store_a.append_record(
        kind="key_rotation",
        namespace="personal",
        source={"agent": "test"},
        payload={
            "new_key_id": f"key_{store_a.cluster_config.node_id}_test",
            "data_key_b64": "W8sHv5Z3Dbe0kX6RiiTy8pquLxC9bpOrQSaUasHQnSU",
            "created_at": "2026-05-08T12:00:00Z",
        },
    )
    sync_cluster(config_a)
    sync_cluster(config_b)

    store_b = ClusterStore.from_config(config_b)
    assert store_b.secret.active_key_id == f"key_{store_a.cluster_config.node_id}_test"
    assert store_b.secret.active_key_hlc == rotation.hlc

    observation = store_b.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-b"},
        payload={"format": "markdown", "body": "- post-rotation", "observed_at": "2026-05-08T12:01:00Z"},
    )
    assert observation.data["encryption"]["key_id"] == store_b.secret.active_key_id


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
    assert '"initialized": true' in result.output
    assert '"cli-node"' in result.output

    result = runner.invoke(cli, ["cluster", "materialize", "--no-reindex"])
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

    result = runner.invoke(cli, ["cluster", "invite", "--expires", "1h"], env=env_a)
    assert result.exit_code == 0, result.output
    token = result.output.strip().splitlines()[-1]
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
