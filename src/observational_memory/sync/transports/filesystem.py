"""Filesystem transport for untrusted shared directories."""

from __future__ import annotations

import json
from pathlib import Path

from ..atomic import atomic_write_bytes, atomic_write_text


class FilesystemTransport:
    name = "filesystem"

    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_heads(self, cluster_id: str) -> dict[str, int]:
        heads: dict[str, int] = {}
        for path in (self._cluster_dir(cluster_id) / "heads").glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            node_id = data.get("node_id") or path.stem
            seq = data.get("seq", 0)
            if isinstance(node_id, str) and isinstance(seq, int):
                heads[node_id] = seq
        return heads

    def read_head(self, cluster_id: str, node_id: str) -> dict | None:
        path = self._cluster_dir(cluster_id) / "heads" / f"{node_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def publish_head(self, cluster_id: str, node_id: str, head: dict) -> None:
        path = self._cluster_dir(cluster_id) / "heads" / f"{node_id}.json"
        atomic_write_text(path, json.dumps(head, indent=2, sort_keys=True) + "\n")

    def publish_node(self, cluster_id: str, node_id: str, data: bytes) -> None:
        path = self._cluster_dir(cluster_id) / "nodes" / f"{node_id}.json"
        atomic_write_bytes(path, data)

    def list_nodes(self, cluster_id: str) -> set[str]:
        return {path.stem for path in (self._cluster_dir(cluster_id) / "nodes").glob("*.json")}

    def fetch_node(self, cluster_id: str, node_id: str) -> bytes | None:
        path = self._cluster_dir(cluster_id) / "nodes" / f"{node_id}.json"
        if not path.exists():
            return None
        return path.read_bytes()

    def list_record_ids(self, cluster_id: str, node_id: str) -> set[str]:
        record_dir = self._cluster_dir(cluster_id) / "records" / node_id
        record_ids: set[str] = set()
        for path in record_dir.glob("*.omr.json"):
            stem = path.name.removesuffix(".omr.json")
            if "-" in stem:
                record_ids.add(stem.split("-", 1)[1])
        return record_ids

    def push_record(self, cluster_id: str, node_id: str, record_id: str, data: bytes) -> None:
        record = json.loads(data.decode("utf-8"))
        seq = int(record["node_seq"])
        path = self._cluster_dir(cluster_id) / "records" / node_id / f"{seq:020d}-{record_id}.omr.json"
        if path.exists():
            return
        atomic_write_bytes(path, data)

    def fetch_record(self, cluster_id: str, node_id: str, record_id: str) -> bytes | None:
        record_dir = self._cluster_dir(cluster_id) / "records" / node_id
        matches = list(record_dir.glob(f"*-{record_id}.omr.json"))
        if not matches:
            return None
        return matches[0].read_bytes()

    def _cluster_dir(self, cluster_id: str) -> Path:
        return self.root / "clusters" / cluster_id
