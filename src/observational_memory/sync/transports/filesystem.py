"""Filesystem transport for untrusted shared directories."""

from __future__ import annotations

import json
from pathlib import Path

from ..atomic import atomic_write_bytes, atomic_write_text
from ..ids import validate_cluster_id, validate_join_request_id, validate_node_id, validate_record_id


class FilesystemTransport:
    name = "filesystem"

    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_heads(self, cluster_id: str) -> dict[str, int]:
        validate_cluster_id(cluster_id)
        heads: dict[str, int] = {}
        for path in (self._cluster_dir(cluster_id) / "heads").glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            node_id = data.get("node_id") or path.stem
            seq = data.get("seq", 0)
            if node_id != path.stem:
                continue
            try:
                validate_node_id(node_id)
            except ValueError:
                continue
            if isinstance(seq, int):
                heads[node_id] = seq
        return heads

    def read_head(self, cluster_id: str, node_id: str) -> dict | None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        path = self._cluster_dir(cluster_id) / "heads" / f"{node_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("node_id") != node_id:
                return None
            return data
        except json.JSONDecodeError:
            return None

    def publish_head(self, cluster_id: str, node_id: str, head: dict) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        if head.get("node_id") != node_id:
            raise ValueError("Head node_id mismatch")
        path = self._cluster_dir(cluster_id) / "heads" / f"{node_id}.json"
        atomic_write_text(path, json.dumps(head, indent=2, sort_keys=True) + "\n")

    def publish_node(self, cluster_id: str, node_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        path = self._cluster_dir(cluster_id) / "nodes" / f"{node_id}.json"
        atomic_write_bytes(path, data)

    def list_nodes(self, cluster_id: str) -> set[str]:
        validate_cluster_id(cluster_id)
        nodes = set()
        for path in (self._cluster_dir(cluster_id) / "nodes").glob("*.json"):
            try:
                nodes.add(validate_node_id(path.stem))
            except ValueError:
                continue
        return nodes

    def fetch_node(self, cluster_id: str, node_id: str) -> bytes | None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        path = self._cluster_dir(cluster_id) / "nodes" / f"{node_id}.json"
        if not path.exists():
            return None
        data = path.read_bytes()
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("node_id") != node_id:
            return None
        return data

    def publish_join_request(self, cluster_id: str, request_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_join_request_id(request_id)
        raw = json.loads(data.decode("utf-8"))
        if raw.get("cluster_id") != cluster_id or raw.get("request_id") != request_id:
            raise ValueError("Join request path metadata mismatch")
        path = self._cluster_dir(cluster_id) / "join-requests" / f"{request_id}.json"
        atomic_write_bytes(path, data)

    def list_join_requests(self, cluster_id: str) -> set[str]:
        validate_cluster_id(cluster_id)
        requests = set()
        for path in (self._cluster_dir(cluster_id) / "join-requests").glob("*.json"):
            try:
                requests.add(validate_join_request_id(path.stem))
            except ValueError:
                continue
        return requests

    def fetch_join_request(self, cluster_id: str, request_id: str) -> bytes | None:
        validate_cluster_id(cluster_id)
        validate_join_request_id(request_id)
        path = self._cluster_dir(cluster_id) / "join-requests" / f"{request_id}.json"
        if not path.exists():
            return None
        data = path.read_bytes()
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("cluster_id") != cluster_id or raw.get("request_id") != request_id:
            return None
        return data

    def publish_join_approval(self, cluster_id: str, request_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_join_request_id(request_id)
        raw = json.loads(data.decode("utf-8"))
        if raw.get("cluster_id") != cluster_id or raw.get("request_id") != request_id:
            raise ValueError("Join approval path metadata mismatch")
        path = self._cluster_dir(cluster_id) / "join-approvals" / f"{request_id}.json"
        atomic_write_bytes(path, data)

    def fetch_join_approval(self, cluster_id: str, request_id: str) -> bytes | None:
        validate_cluster_id(cluster_id)
        validate_join_request_id(request_id)
        path = self._cluster_dir(cluster_id) / "join-approvals" / f"{request_id}.json"
        if not path.exists():
            return None
        data = path.read_bytes()
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("cluster_id") != cluster_id or raw.get("request_id") != request_id:
            return None
        return data

    def list_record_ids(self, cluster_id: str, node_id: str) -> set[str]:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        record_dir = self._cluster_dir(cluster_id) / "records" / node_id
        record_ids: set[str] = set()
        for path in record_dir.glob("*.omr.json"):
            stem = path.name.removesuffix(".omr.json")
            if "-" in stem:
                try:
                    record_ids.add(validate_record_id(stem.split("-", 1)[1]))
                except ValueError:
                    continue
        return record_ids

    def push_record(self, cluster_id: str, node_id: str, record_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        validate_record_id(record_id)
        record = json.loads(data.decode("utf-8"))
        seq = int(record["node_seq"])
        if (
            record.get("cluster_id") != cluster_id
            or record.get("node_id") != node_id
            or record.get("record_id") != record_id
        ):
            raise ValueError("Record path metadata mismatch")
        path = self._cluster_dir(cluster_id) / "records" / node_id / f"{seq:020d}-{record_id}.omr.json"
        if path.exists():
            return
        atomic_write_bytes(path, data)

    def fetch_record(self, cluster_id: str, node_id: str, record_id: str) -> bytes | None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        validate_record_id(record_id)
        record_dir = self._cluster_dir(cluster_id) / "records" / node_id
        matches = list(record_dir.glob(f"*-{record_id}.omr.json"))
        if not matches:
            return None
        data = matches[0].read_bytes()
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("cluster_id") != cluster_id or raw.get("node_id") != node_id or raw.get("record_id") != record_id:
            return None
        return data

    def _cluster_dir(self, cluster_id: str) -> Path:
        validate_cluster_id(cluster_id)
        return self.root / "clusters" / cluster_id
