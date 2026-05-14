"""HTTP relay transport for opaque OM Cluster artifacts."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..ids import validate_cluster_id, validate_join_request_id, validate_node_id, validate_record_id


class RelayTransport:
    name = "relay"

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_heads(self, cluster_id: str) -> dict[str, int]:
        data = self._get_json(f"/v1/clusters/{_q_cluster(cluster_id)}/heads")
        if not isinstance(data, dict):
            return {}
        heads: dict[str, int] = {}
        for node_id, seq in data.items():
            try:
                validate_node_id(str(node_id))
            except ValueError:
                continue
            if isinstance(seq, int):
                heads[str(node_id)] = seq
        return heads

    def read_head(self, cluster_id: str, node_id: str) -> dict | None:
        data = self._get_json(f"/v1/clusters/{_q_cluster(cluster_id)}/heads/{_q_node(node_id)}")
        if not isinstance(data, dict) or data.get("node_id") != node_id:
            return None
        return data

    def publish_head(self, cluster_id: str, node_id: str, head: dict) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        if head.get("node_id") != node_id:
            raise ValueError("Head node_id mismatch")
        self._put_json(f"/v1/clusters/{_q_cluster(cluster_id)}/heads/{_q_node(node_id)}", head)

    def publish_node(self, cluster_id: str, node_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        raw = json.loads(data.decode("utf-8"))
        if raw.get("node_id") != node_id:
            raise ValueError("Node metadata path mismatch")
        self._put_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/nodes/{_q_node(node_id)}", data)

    def list_nodes(self, cluster_id: str) -> set[str]:
        data = self._get_json(f"/v1/clusters/{_q_cluster(cluster_id)}/nodes")
        if not isinstance(data, list):
            return set()
        nodes = set()
        for node_id in data:
            try:
                nodes.add(validate_node_id(str(node_id)))
            except ValueError:
                continue
        return nodes

    def fetch_node(self, cluster_id: str, node_id: str) -> bytes | None:
        data = self._get_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/nodes/{_q_node(node_id)}")
        if data is None:
            return None
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
        self._put_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/join-requests/{_q_join(request_id)}", data)

    def list_join_requests(self, cluster_id: str) -> set[str]:
        data = self._get_json(f"/v1/clusters/{_q_cluster(cluster_id)}/join-requests")
        if not isinstance(data, list):
            return set()
        requests = set()
        for request_id in data:
            try:
                requests.add(validate_join_request_id(str(request_id)))
            except ValueError:
                continue
        return requests

    def fetch_join_request(self, cluster_id: str, request_id: str) -> bytes | None:
        data = self._get_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/join-requests/{_q_join(request_id)}")
        if data is None:
            return None
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
        self._put_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/join-approvals/{_q_join(request_id)}", data)

    def fetch_join_approval(self, cluster_id: str, request_id: str) -> bytes | None:
        data = self._get_bytes(f"/v1/clusters/{_q_cluster(cluster_id)}/join-approvals/{_q_join(request_id)}")
        if data is None:
            return None
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("cluster_id") != cluster_id or raw.get("request_id") != request_id:
            return None
        return data

    def list_record_ids(self, cluster_id: str, node_id: str) -> set[str]:
        data = self._get_json(f"/v1/clusters/{_q_cluster(cluster_id)}/records/{_q_node(node_id)}")
        if not isinstance(data, list):
            return set()
        record_ids = set()
        for record_id in data:
            try:
                record_ids.add(validate_record_id(str(record_id)))
            except ValueError:
                continue
        return record_ids

    def push_record(self, cluster_id: str, node_id: str, record_id: str, data: bytes) -> None:
        validate_cluster_id(cluster_id)
        validate_node_id(node_id)
        validate_record_id(record_id)
        record = json.loads(data.decode("utf-8"))
        if (
            record.get("cluster_id") != cluster_id
            or record.get("node_id") != node_id
            or record.get("record_id") != record_id
        ):
            raise ValueError("Record path metadata mismatch")
        self._put_bytes(
            f"/v1/clusters/{_q_cluster(cluster_id)}/records/{_q_node(node_id)}/{_q_record(record_id)}",
            data,
        )

    def fetch_record(self, cluster_id: str, node_id: str, record_id: str) -> bytes | None:
        data = self._get_bytes(
            f"/v1/clusters/{_q_cluster(cluster_id)}/records/{_q_node(node_id)}/{_q_record(record_id)}"
        )
        if data is None:
            return None
        try:
            raw = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        if raw.get("cluster_id") != cluster_id or raw.get("node_id") != node_id or raw.get("record_id") != record_id:
            return None
        return data

    def _get_json(self, path: str):
        data = self._get_bytes(path)
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))

    def _put_json(self, path: str, value: dict) -> None:
        self._put_bytes(path, json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n")

    def _get_bytes(self, path: str) -> bytes | None:
        request = Request(self.base_url + path, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read()
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _put_bytes(self, path: str, data: bytes) -> None:
        request = Request(
            self.base_url + path,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            response.read()


def _q_cluster(cluster_id: str) -> str:
    return quote(validate_cluster_id(cluster_id), safe="")


def _q_node(node_id: str) -> str:
    return quote(validate_node_id(node_id), safe="")


def _q_record(record_id: str) -> str:
    return quote(validate_record_id(record_id), safe="")


def _q_join(request_id: str) -> str:
    return quote(validate_join_request_id(request_id), safe="")
