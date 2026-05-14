"""Cluster sync engine."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from observational_memory.config import Config

from .config import TransportConfig, apply_join_approval, load_cluster_config, load_pending_join_state
from .materialize import materialize_cluster_memory
from .store import ClusterStore
from .transports import SyncTransport
from .transports.filesystem import FilesystemTransport
from .transports.relay import RelayTransport


@dataclass(frozen=True)
class TransportSummary:
    name: str
    pulled: int = 0
    pushed: int = 0
    skipped: int = 0
    rejected: int = 0
    error: str | None = None


@dataclass(frozen=True)
class SyncSummary:
    pulled: int = 0
    pushed: int = 0
    skipped: int = 0
    rejected: int = 0
    materialized: bool = False
    transports: list[TransportSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pulled": self.pulled,
            "pushed": self.pushed,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "materialized": self.materialized,
            "transports": [transport.__dict__ for transport in self.transports],
        }


def sync_cluster(
    config: Config,
    *,
    deadline_ms: int | None = None,
    pull_only: bool = False,
    materialize: bool = True,
) -> SyncSummary:
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise RuntimeError("OM Cluster is not initialized")
    try:
        store = ClusterStore.from_config(config)
    except FileNotFoundError as e:
        join_status = _complete_pending_join(config, cluster_config)
        if join_status != "approved":
            raise RuntimeError("OM Cluster join request is pending approval") from e
        store = ClusterStore.from_config(config)
    store.ensure_layout()
    deadline = time.monotonic() + (deadline_ms / 1000) if deadline_ms is not None else None

    summaries: list[TransportSummary] = []
    for transport_config in cluster_config.transports:
        if deadline is not None and time.monotonic() >= deadline:
            break
        transport = build_transport(transport_config)
        summaries.append(_sync_transport(store, transport, deadline=deadline, pull_only=pull_only))

    pulled = sum(item.pulled for item in summaries)
    pushed = sum(item.pushed for item in summaries)
    skipped = sum(item.skipped for item in summaries)
    rejected = sum(item.rejected for item in summaries)
    materialized = False
    if materialize and pulled:
        materialized = materialize_cluster_memory(config, store).any_written
    return SyncSummary(
        pulled=pulled,
        pushed=pushed,
        skipped=skipped,
        rejected=rejected,
        materialized=materialized,
        transports=summaries,
    )


def build_transport(config: TransportConfig) -> SyncTransport:
    if config.type == "filesystem" and config.path:
        return FilesystemTransport(Path(config.path))
    if config.type == "relay" and config.path:
        return RelayTransport(config.path)
    raise ValueError(f"Unsupported transport: {config.type}")


def _sync_transport(
    store: ClusterStore,
    transport: SyncTransport,
    *,
    deadline: float | None,
    pull_only: bool,
) -> TransportSummary:
    pulled = pushed = skipped = rejected = 0
    try:
        _publish_known_nodes(store, transport)
        _pull_public_node_metadata(store, transport)
        if not pull_only:
            pushed += _push_records(store, transport)
        for node_id in sorted(_remote_node_ids(store, transport)):
            if deadline is not None and time.monotonic() >= deadline:
                break
            pending: list[tuple[int, int, str, bytes]] = []
            for record_id in transport.list_record_ids(store.cluster_config.id, node_id):
                if store_has_record(store, record_id):
                    skipped += 1
                    continue
                data = transport.fetch_record(store.cluster_config.id, node_id, record_id)
                if data is None:
                    skipped += 1
                    continue
                try:
                    raw = json.loads(data.decode("utf-8"))
                    membership_rank = 0 if raw.get("kind") == "node_membership" else 1
                    seq = int(raw.get("node_seq", 0))
                except Exception:
                    membership_rank = 2
                    seq = 0
                pending.append((membership_rank, seq, record_id, data))
            for _membership_rank, _seq, _record_id, data in sorted(pending):
                result = store.import_record_bytes(data)
                if result.imported:
                    pulled += 1
                elif result.status == "duplicate":
                    skipped += 1
                else:
                    rejected += 1
        _publish_heads(store, transport)
        if not pull_only:
            # Push again after pull so membership records imported earlier in
            # this sync are available to peers on the same manual run.
            pushed += _push_records(store, transport)
    except Exception as e:
        return TransportSummary(transport.name, pulled, pushed, skipped, rejected, error=str(e))
    return TransportSummary(transport.name, pulled, pushed, skipped, rejected)


def _complete_pending_join(config: Config, cluster_config) -> str | None:
    state = load_pending_join_state(config, cluster_config.id)
    if not state:
        return None
    request = state.get("join_request")
    request_id = state.get("request_id")
    if not isinstance(request, dict) or not isinstance(request_id, str):
        return None
    status = None
    for transport_config in cluster_config.transports:
        transport = build_transport(transport_config)
        try:
            transport.publish_join_request(
                cluster_config.id,
                request_id,
                (json.dumps(request, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            approval_bytes = transport.fetch_join_approval(cluster_config.id, request_id)
        except Exception:
            continue
        if approval_bytes is None:
            continue
        try:
            status = apply_join_approval(config, json.loads(approval_bytes.decode("utf-8")))
        except Exception:
            continue
        if status in {"approved", "rejected"}:
            return status
    return status


def _publish_known_nodes(store: ClusterStore, transport: SyncTransport) -> None:
    for node in store.public_nodes().values():
        transport.publish_node(
            store.cluster_config.id,
            node.node_id,
            (json.dumps(node.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )


def _publish_heads(store: ClusterStore, transport: SyncTransport) -> None:
    for node_id in store.all_heads():
        head = store._read_head(node_id)
        if head:
            transport.publish_head(store.cluster_config.id, node_id, head)


def _pull_public_node_metadata(store: ClusterStore, transport: SyncTransport) -> None:
    for node_id in transport.list_nodes(store.cluster_config.id):
        data = transport.fetch_node(store.cluster_config.id, node_id)
        if data is not None:
            store.import_node_metadata_bytes(data)


def _remote_node_ids(store: ClusterStore, transport: SyncTransport) -> set[str]:
    nodes = set(transport.list_nodes(store.cluster_config.id))
    nodes.update(transport.list_heads(store.cluster_config.id))
    return nodes


def _push_records(store: ClusterStore, transport: SyncTransport) -> int:
    pushed = 0
    remote_cache: dict[str, set[str]] = {}
    for record in store.list_records(include_tombstoned=True):
        remote_ids = remote_cache.setdefault(
            record.node_id,
            transport.list_record_ids(store.cluster_config.id, record.node_id),
        )
        if record.record_id in remote_ids:
            continue
        transport.push_record(store.cluster_config.id, record.node_id, record.record_id, store.record_bytes(record))
        remote_ids.add(record.record_id)
        pushed += 1
    return pushed


def store_has_record(store: ClusterStore, record_id: str) -> bool:
    return store._record_path_by_id(record_id) is not None
