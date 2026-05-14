"""Direct peer HTTP transport for OM Cluster."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from .relay import RelayTransport

T = TypeVar("T")


class P2PTransport:
    name = "p2p"

    def __init__(self, peers: str, *, timeout_seconds: float = 5.0):
        urls = [peer.strip() for peer in peers.split(",") if peer.strip()]
        if not urls:
            raise ValueError("P2P transport requires at least one peer URL")
        self.peers = [RelayTransport(url, timeout_seconds=timeout_seconds) for url in urls]

    def list_heads(self, cluster_id: str) -> dict[str, int]:
        heads: dict[str, int] = {}
        for peer_heads in self._collect(lambda peer: peer.list_heads(cluster_id), default={}):
            heads.update(peer_heads)
        return heads

    def read_head(self, cluster_id: str, node_id: str) -> dict | None:
        return self._first(lambda peer: peer.read_head(cluster_id, node_id))

    def publish_head(self, cluster_id: str, node_id: str, head: dict) -> None:
        self._publish(lambda peer: peer.publish_head(cluster_id, node_id, head))

    def publish_node(self, cluster_id: str, node_id: str, data: bytes) -> None:
        self._publish(lambda peer: peer.publish_node(cluster_id, node_id, data))

    def list_nodes(self, cluster_id: str) -> set[str]:
        nodes: set[str] = set()
        for peer_nodes in self._collect(lambda peer: peer.list_nodes(cluster_id), default=set()):
            nodes.update(peer_nodes)
        return nodes

    def fetch_node(self, cluster_id: str, node_id: str) -> bytes | None:
        return self._first(lambda peer: peer.fetch_node(cluster_id, node_id))

    def publish_join_request(self, cluster_id: str, request_id: str, data: bytes) -> None:
        self._publish(lambda peer: peer.publish_join_request(cluster_id, request_id, data))

    def list_join_requests(self, cluster_id: str) -> set[str]:
        requests: set[str] = set()
        for peer_requests in self._collect(lambda peer: peer.list_join_requests(cluster_id), default=set()):
            requests.update(peer_requests)
        return requests

    def fetch_join_request(self, cluster_id: str, request_id: str) -> bytes | None:
        return self._first(lambda peer: peer.fetch_join_request(cluster_id, request_id))

    def publish_join_approval(self, cluster_id: str, request_id: str, data: bytes) -> None:
        self._publish(lambda peer: peer.publish_join_approval(cluster_id, request_id, data))

    def fetch_join_approval(self, cluster_id: str, request_id: str) -> bytes | None:
        return self._first(lambda peer: peer.fetch_join_approval(cluster_id, request_id))

    def list_record_ids(self, cluster_id: str, node_id: str) -> set[str]:
        record_ids: set[str] = set()
        for peer_records in self._collect(lambda peer: peer.list_record_ids(cluster_id, node_id), default=set()):
            record_ids.update(peer_records)
        return record_ids

    def push_record(self, cluster_id: str, node_id: str, record_id: str, data: bytes) -> None:
        self._publish(lambda peer: peer.push_record(cluster_id, node_id, record_id, data))

    def fetch_record(self, cluster_id: str, node_id: str, record_id: str) -> bytes | None:
        return self._first(lambda peer: peer.fetch_record(cluster_id, node_id, record_id))

    def _collect(self, fn: Callable[[RelayTransport], T], *, default: T) -> list[T]:
        values: list[T] = []
        for peer in self.peers:
            try:
                values.append(fn(peer))
            except Exception:
                values.append(default)
        return values

    def _first(self, fn: Callable[[RelayTransport], T | None]) -> T | None:
        for peer in self.peers:
            try:
                value = fn(peer)
            except Exception:
                continue
            if value is not None:
                return value
        return None

    def _publish(self, fn: Callable[[RelayTransport], None]) -> None:
        errors = []
        for peer in self.peers:
            try:
                fn(peer)
            except Exception as e:
                errors.append(e)
        if len(errors) == len(self.peers):
            raise errors[-1]
