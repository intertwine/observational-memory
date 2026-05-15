import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from observational_memory.config import Config
from observational_memory.sync.config import (
    TransportConfig,
    create_invite_token,
    initialize_cluster_config,
    join_cluster_from_invite,
)
from observational_memory.sync.engine import sync_cluster
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.relay_server import scan_relay_artifacts, serve_relay
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


def test_two_nodes_converge_over_relay_transport(tmp_path):
    with _relay_server() as relay:
        config_a = _node_config(tmp_path, "a")
        cluster_a = initialize_cluster_config(
            config_a,
            name="Cluster",
            node_alias="node-a",
            transports=[TransportConfig(type="relay", path=relay.url)],
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
            payload={"format": "markdown", "body": "- relay memory from A", "observed_at": "2026-05-08T12:00:00Z"},
        )
        store_b.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "codex", "host_alias": "node-b"},
            payload={"format": "markdown", "body": "- relay memory from B", "observed_at": "2026-05-08T12:01:00Z"},
        )

        sync_cluster(config_a)
        sync_cluster(config_b)
        sync_cluster(config_a)
        sync_cluster(config_b)
        materialize_cluster_memory(config_a, ClusterStore.from_config(config_a))
        materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))

        assert "relay memory from A" in config_a.observations_path.read_text()
        assert "relay memory from B" in config_a.observations_path.read_text()
        assert "relay memory from A" in config_b.observations_path.read_text()
        assert "relay memory from B" in config_b.observations_path.read_text()
        relay_bytes = b"".join(relay.storage.values())
        assert b"relay memory from A" not in relay_bytes
        assert b"relay memory from B" not in relay_bytes
        assert b"signing_private_key_b64" not in relay_bytes
        assert b"data_keys" not in relay_bytes


def test_pending_peer_metadata_is_removed_after_relay_approval(tmp_path):
    with _relay_server() as relay:
        config_a = _node_config(tmp_path, "a")
        cluster_a = initialize_cluster_config(
            config_a,
            name="Cluster",
            node_alias="node-a",
            transports=[TransportConfig(type="relay", path=relay.url)],
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
        assert store_b.cluster_config.node_id in store_a.public_nodes()
        assert store_b.cluster_config.node_id not in store_a.pending_nodes()


def test_supported_relay_server_health_and_secret_scan(tmp_path):
    storage = tmp_path / "relay"
    server = serve_relay(storage, host="127.0.0.1", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_a = _node_config(tmp_path, "a")
        cluster_a = initialize_cluster_config(
            config_a,
            name="Cluster",
            node_alias="node-a",
            transports=[TransportConfig(type="relay", path=f"http://{host}:{port}")],
        )
        store_a = ClusterStore.from_config(config_a)
        store_a.ensure_layout()
        _membership(store_a)
        store_a.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "codex", "host_alias": "node-a"},
            payload={"format": "markdown", "body": "- server relay memory", "observed_at": "2026-05-08T12:00:00Z"},
        )

        summary = sync_cluster(config_a)

        assert summary.transports[0].error is None
        scan = scan_relay_artifacts(storage)
        assert scan["ok"] is True
        assert scan["file_count"] > 0
        relay_bytes = b"".join(path.read_bytes() for path in storage.rglob("*") if path.is_file())
        assert b"server relay memory" not in relay_bytes
        assert cluster_a.id
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_relay_outage_is_reported_without_losing_local_records(tmp_path):
    config_a = _node_config(tmp_path, "a")
    initialize_cluster_config(
        config_a,
        name="Cluster",
        node_alias="node-a",
        transports=[TransportConfig(type="relay", path="http://127.0.0.1:9")],
    )
    store_a = ClusterStore.from_config(config_a)
    store_a.ensure_layout()
    _membership(store_a)
    record = store_a.append_record(
        kind="observation",
        namespace="personal",
        source={"agent": "codex", "host_alias": "node-a"},
        payload={"format": "markdown", "body": "- local-first memory", "observed_at": "2026-05-08T12:00:00Z"},
    )

    summary = sync_cluster(config_a)

    assert summary.transports[0].error
    assert ClusterStore.from_config(config_a)._record_path_by_id(record.record_id) is not None


def test_malformed_relay_data_fails_closed(tmp_path):
    with _relay_server() as relay:
        config_a = _node_config(tmp_path, "a")
        cluster_a = initialize_cluster_config(
            config_a,
            name="Cluster",
            node_alias="node-a",
            transports=[TransportConfig(type="relay", path=relay.url)],
        )
        store_a = ClusterStore.from_config(config_a)
        store_a.ensure_layout()
        _membership(store_a)
        relay.storage[f"{cluster_a.id}/nodes/node_bad"] = b"{"

        summary = sync_cluster(config_a)

        assert summary.pulled == 0
        assert "node_bad" not in ClusterStore.from_config(config_a).public_nodes()


class _Relay:
    def __enter__(self):
        self.storage: dict[str, bytes] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        self.server.storage = self.storage
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.thread.join(timeout=5)


def _relay_server():
    return _Relay()


class _RelayHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_GET(self):
        parts = self._parts()
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "clusters" and parts[3] == "heads":
            return self._send_json(self._heads(parts[2]))
        if len(parts) == 5 and parts[3] == "heads":
            return self._send_stored(f"{parts[2]}/heads/{parts[4]}")
        if len(parts) == 4 and parts[3] == "nodes":
            return self._send_json(self._ids(f"{parts[2]}/nodes/"))
        if len(parts) == 5 and parts[3] == "nodes":
            return self._send_stored(f"{parts[2]}/nodes/{parts[4]}")
        if len(parts) == 4 and parts[3] == "join-requests":
            return self._send_json(self._ids(f"{parts[2]}/join-requests/"))
        if len(parts) == 5 and parts[3] == "join-requests":
            return self._send_stored(f"{parts[2]}/join-requests/{parts[4]}")
        if len(parts) == 5 and parts[3] == "join-approvals":
            return self._send_stored(f"{parts[2]}/join-approvals/{parts[4]}")
        if len(parts) == 5 and parts[3] == "records":
            return self._send_json(self._ids(f"{parts[2]}/records/{parts[4]}/"))
        if len(parts) == 6 and parts[3] == "records":
            return self._send_stored(f"{parts[2]}/records/{parts[4]}/{parts[5]}")
        self.send_error(404)

    def do_PUT(self):
        parts = self._parts()
        if len(parts) == 5 and parts[3] in {"heads", "nodes", "join-requests", "join-approvals"}:
            return self._store(f"{parts[2]}/{parts[3]}/{parts[4]}")
        if len(parts) == 6 and parts[3] == "records":
            return self._store(f"{parts[2]}/records/{parts[4]}/{parts[5]}")
        self.send_error(404)

    def log_message(self, _format, *_args):
        return

    def _parts(self) -> list[str]:
        return [part for part in self.path.split("?", 1)[0].split("/") if part]

    def _store(self, key: str):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.storage[key] = self.rfile.read(length)
        self.send_response(204)
        self.end_headers()

    def _send_stored(self, key: str):
        data = self.server.storage.get(key)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, value):
        data = json.dumps(value, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _ids(self, prefix: str) -> list[str]:
        ids = []
        for key in self.server.storage:
            if key.startswith(prefix):
                suffix = key.removeprefix(prefix)
                if "/" not in suffix:
                    ids.append(suffix)
        return sorted(ids)

    def _heads(self, cluster_id: str) -> dict[str, int]:
        heads = {}
        for node_id in self._ids(f"{cluster_id}/heads/"):
            try:
                heads[node_id] = int(json.loads(self.server.storage[f"{cluster_id}/heads/{node_id}"])["seq"])
            except Exception:
                continue
        return heads
