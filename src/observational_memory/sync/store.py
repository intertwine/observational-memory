"""Local append-only OM Cluster record store."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observational_memory.config import Config

from .atomic import DirectoryLock, atomic_write_bytes, atomic_write_text
from .clock import HybridLogicalTimestamp, merge, parse_hlc, tick
from .config import (
    ClusterConfig,
    add_cluster_data_key,
    load_cluster_config,
    load_cluster_secret,
    load_node_keypair,
)
from .crypto import ClusterSecret, NodeKeypair, b64url_encode
from .frontier import frontier_from_records
from .ids import validate_cluster_id, validate_node_id, validate_record_id
from .records import RecordEnvelope, create_record, decrypt_record_payload, record_path_name, verify_record_envelope

_MAX_PENDING_NODE_METADATA = 128


@dataclass(frozen=True)
class ImportResult:
    status: str
    record_id: str | None = None
    reason: str | None = None

    @property
    def imported(self) -> bool:
        return self.status == "imported"


@dataclass(frozen=True)
class NodeMetadata:
    node_id: str
    alias: str
    signing_public_key_b64: str
    revoked: bool = False
    revoked_after_hlc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "node_id": self.node_id,
            "alias": self.alias,
            "signing_public_key_b64": self.signing_public_key_b64,
            "revoked": self.revoked,
            "revoked_after_hlc": self.revoked_after_hlc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeMetadata:
        validate_node_id(data["node_id"])
        return cls(
            node_id=data["node_id"],
            alias=data.get("alias", data["node_id"]),
            signing_public_key_b64=data["signing_public_key_b64"],
            revoked=bool(data.get("revoked", False)),
            revoked_after_hlc=data.get("revoked_after_hlc"),
        )


class ClusterStore:
    """Append-only local record store with verification and materialization state."""

    def __init__(
        self,
        config: Config,
        cluster_config: ClusterConfig,
        keypair: NodeKeypair,
        secret: ClusterSecret,
    ):
        self.config = config
        self.cluster_config = cluster_config
        self.keypair = keypair
        self.secret = secret

    @classmethod
    def from_config(cls, config: Config) -> ClusterStore:
        cluster_config = load_cluster_config(config)
        if cluster_config is None:
            raise RuntimeError("OM Cluster is not initialized")
        keypair = load_node_keypair(config, cluster_config)
        secret = load_cluster_secret(config, cluster_config.id)
        return cls(config, cluster_config, keypair, secret)

    @property
    def cluster_dir(self) -> Path:
        validate_cluster_id(self.cluster_config.id)
        return self.config.clusters_dir / self.cluster_config.id

    @property
    def records_dir(self) -> Path:
        return self.cluster_dir / "records"

    @property
    def heads_dir(self) -> Path:
        return self.cluster_dir / "heads"

    @property
    def nodes_dir(self) -> Path:
        return self.cluster_dir / "nodes"

    @property
    def pending_nodes_dir(self) -> Path:
        return self.cluster_dir / "pending-nodes"

    @property
    def diagnostics_path(self) -> Path:
        return self.cluster_dir / "diagnostics.jsonl"

    @property
    def materializer_state_path(self) -> Path:
        return self.cluster_dir / "materializer-state.json"

    @property
    def record_index_path(self) -> Path:
        return self.cluster_dir / "index" / "records.json"

    @property
    def sync_lock_path(self) -> Path:
        return self.cluster_dir / ".locks" / "sync.lock"

    def ensure_layout(self) -> None:
        for path in (
            self.records_dir,
            self.heads_dir,
            self.nodes_dir,
            self.pending_nodes_dir,
            self.cluster_dir / "tombstones",
            self.record_index_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.write_node_metadata(
            NodeMetadata(
                node_id=self.keypair.node_id,
                alias=self.cluster_config.node_alias,
                signing_public_key_b64=self.keypair.signing_public_key_b64,
            )
        )

    def append_record(
        self,
        *,
        kind: str,
        namespace: str | None = None,
        source: dict[str, Any] | None = None,
        payload: dict[str, Any],
    ) -> RecordEnvelope:
        self.ensure_layout()
        with DirectoryLock(self.sync_lock_path):
            self._reload_secret()
            node_seq = self.all_heads().get(self.keypair.node_id, 0) + 1
            hlc = self._next_hlc()
            record = create_record(
                cluster_id=self.cluster_config.id,
                keypair=self.keypair,
                secret=self.secret,
                kind=kind,
                namespace=namespace or self.cluster_config.default_namespace,
                node_seq=node_seq,
                hlc=str(hlc),
                parents=self.all_heads(),
                source=source or {},
                payload=payload,
            )
            self._write_record(record)
            self._write_head(record.node_id, record.node_seq, record.record_id)
            if kind == "node_membership":
                self._apply_membership_record(record, payload)
            if kind == "key_rotation":
                self._apply_key_rotation_payload(record, payload)
            return record

    def import_record_bytes(self, data: bytes) -> ImportResult:
        try:
            record = RecordEnvelope.from_bytes(data)
            validate_record_id(record.record_id)
            validate_node_id(record.node_id)
        except Exception as e:
            self._record_diagnostic("rejected", None, f"invalid JSON: {e}")
            return ImportResult("rejected", reason=f"invalid JSON: {e}")

        existing_path = self._record_path_by_id(record.record_id)
        if existing_path is not None:
            if existing_path.read_bytes().strip() == data.strip():
                return ImportResult("duplicate", record_id=record.record_id)
            self._record_diagnostic("rejected", record.record_id, "duplicate record_id with different bytes")
            return ImportResult(
                "rejected",
                record_id=record.record_id,
                reason="duplicate record_id with different bytes",
            )

        try:
            self._reload_secret()
            public_key = self._public_key_for_record(record)
            verify_record_envelope(record, cluster_id=self.cluster_config.id, signing_public_key_b64=public_key)
            payload = decrypt_record_payload(record, secret=self.secret)
            self._reject_if_revoked_future(record)
        except Exception as e:
            self._record_diagnostic("rejected", record.record_id, str(e))
            return ImportResult("rejected", record_id=record.record_id, reason=str(e))

        try:
            self._write_record(record)
            self._update_imported_head(record)
            if record.kind == "node_membership":
                self._apply_membership_record(record, payload)
            if record.kind == "key_rotation":
                self._apply_key_rotation_payload(record, payload)
            self._merge_clock(record.hlc)
        except Exception as e:
            self._record_diagnostic("rejected", record.record_id, f"storage error: {e}")
            return ImportResult("rejected", record_id=record.record_id, reason=f"storage error: {e}")
        return ImportResult("imported", record_id=record.record_id)

    def list_records(self, kind: str | None = None, include_tombstoned: bool = False) -> list[RecordEnvelope]:
        records: list[RecordEnvelope] = []
        if not self.records_dir.exists():
            return []
        tombstoned = set() if include_tombstoned else self.tombstoned_record_ids()
        for path in self.records_dir.glob("*/*.omr.json"):
            try:
                record = RecordEnvelope.from_bytes(path.read_bytes())
            except Exception:
                continue
            if kind is not None and record.kind != kind:
                continue
            if record.record_id in tombstoned:
                continue
            records.append(record)
        return sorted(records, key=lambda r: (r.hlc, r.node_id, r.node_seq, r.record_id))

    def rebuild_record_index(self) -> dict[str, Any]:
        records: dict[str, Any] = {}
        if self.records_dir.exists():
            for path in self.records_dir.glob("*/*.omr.json"):
                try:
                    record = RecordEnvelope.from_bytes(path.read_bytes())
                except Exception:
                    continue
                records[record.record_id] = self._index_entry(record, path)
        index = {
            "version": 1,
            "cluster_id": self.cluster_config.id,
            "records": records,
            "rebuilt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        atomic_write_text(self.record_index_path, json.dumps(index, indent=2, sort_keys=True) + "\n")
        return index

    def record_bytes(self, record: RecordEnvelope) -> bytes:
        return record.to_bytes()

    def read_payload(self, record: RecordEnvelope) -> dict[str, Any]:
        self._reload_secret()
        return decrypt_record_payload(record, secret=self.secret)

    def local_head(self) -> dict[str, Any]:
        return self._read_head(self.keypair.node_id) or {"node_id": self.keypair.node_id, "seq": 0}

    def all_heads(self) -> dict[str, int]:
        heads: dict[str, int] = {}
        if not self.heads_dir.exists():
            return heads
        for path in self.heads_dir.glob("*.json"):
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
                heads[node_id] = max(heads.get(node_id, 0), seq)
        return heads

    def public_nodes(self) -> dict[str, NodeMetadata]:
        nodes: dict[str, NodeMetadata] = {}
        if not self.nodes_dir.exists():
            return nodes
        for path in self.nodes_dir.glob("*.json"):
            try:
                node = NodeMetadata.from_dict(json.loads(path.read_text()))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if node.node_id != path.stem:
                continue
            nodes[node.node_id] = node
        return nodes

    def pending_nodes(self) -> dict[str, NodeMetadata]:
        nodes: dict[str, NodeMetadata] = {}
        if not self.pending_nodes_dir.exists():
            return nodes
        for path in self.pending_nodes_dir.glob("*.json"):
            try:
                node = NodeMetadata.from_dict(json.loads(path.read_text()))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if node.node_id != path.stem:
                continue
            nodes[node.node_id] = node
        return nodes

    def write_node_metadata(self, metadata: NodeMetadata) -> None:
        atomic_write_text(
            self._node_metadata_path(self.nodes_dir, metadata.node_id),
            json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n",
        )

    def import_node_metadata_bytes(self, data: bytes) -> bool:
        try:
            metadata = NodeMetadata.from_dict(json.loads(data.decode("utf-8")))
        except Exception:
            return False
        if metadata.node_id in self.public_nodes():
            return False
        # Public metadata alone is not trust. Keep it separate so diagnostics can
        # show pending/unknown nodes without authorizing their records.
        self.pending_nodes_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = self._node_metadata_path(self.pending_nodes_dir, metadata.node_id)
        except ValueError:
            return False
        content = json.dumps(metadata.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if path.exists() and path.read_bytes() == content:
            return False
        if not path.exists() and len(list(self.pending_nodes_dir.glob("*.json"))) >= _MAX_PENDING_NODE_METADATA:
            self._record_diagnostic(
                "rejected",
                None,
                f"pending node metadata cap reached ({_MAX_PENDING_NODE_METADATA})",
            )
            return False
        atomic_write_bytes(path, content)
        return True

    def tombstoned_record_ids(self) -> set[str]:
        tombstoned: set[str] = set()
        for record in self.list_records(kind="tombstone", include_tombstoned=True):
            try:
                payload = self.read_payload(record)
            except Exception:
                continue
            target = payload.get("target_record_id")
            if isinstance(target, str):
                tombstoned.add(target)
        return tombstoned

    def records_frontier(self) -> dict[str, int]:
        return frontier_from_records(self.list_records(include_tombstoned=True))

    def _write_record(self, record: RecordEnvelope) -> None:
        validate_node_id(record.node_id)
        validate_record_id(record.record_id)
        if record.cluster_id != self.cluster_config.id:
            raise ValueError("Record cluster_id mismatch")
        path = self.records_dir / record.node_id / record_path_name(record)
        if path.exists():
            return
        seq_conflict = list((self.records_dir / record.node_id).glob(f"{record.node_seq:020d}-*.omr.json"))
        if seq_conflict and all(record.record_id not in p.name for p in seq_conflict):
            raise ValueError(f"Sequence conflict for {record.node_id} seq {record.node_seq}")
        atomic_write_bytes(path, record.to_bytes())
        self._update_record_index(record, path)

    def _write_head(self, node_id: str, seq: int, record_id: str | None) -> None:
        validate_node_id(node_id)
        if record_id is not None:
            validate_record_id(record_id)
        data = {
            "version": 1,
            "cluster_id": self.cluster_config.id,
            "node_id": node_id,
            "seq": seq,
            "record_id": record_id,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        atomic_write_text(self.heads_dir / f"{node_id}.json", json.dumps(data, indent=2, sort_keys=True) + "\n")

    def _read_head(self, node_id: str) -> dict[str, Any] | None:
        validate_node_id(node_id)
        path = self.heads_dir / f"{node_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data.get("node_id") != node_id:
            return None
        return data

    @staticmethod
    def _node_metadata_path(directory: Path, node_id: str) -> Path:
        validate_node_id(node_id)
        return directory / f"{node_id}.json"

    def _has_record(self, record_id: str) -> bool:
        return self._record_path_by_id(record_id) is not None

    def _record_path_by_id(self, record_id: str) -> Path | None:
        validate_record_id(record_id)
        if not self.records_dir.exists():
            return None
        path = self._record_path_from_index(record_id)
        if path is not None:
            return path
        return next(self.records_dir.glob(f"*/*-{record_id}.omr.json"), None)

    def _record_path_from_index(self, record_id: str) -> Path | None:
        if not self.record_index_path.exists():
            return None
        try:
            index = json.loads(self.record_index_path.read_text())
            entry = index.get("records", {}).get(record_id)
            if not isinstance(entry, dict):
                return None
            path = Path(entry["path"])
        except Exception:
            return None
        if path.exists() and path.name.endswith(f"-{record_id}.omr.json"):
            return path
        return None

    def _update_record_index(self, record: RecordEnvelope, path: Path) -> None:
        try:
            if self.record_index_path.exists():
                index = json.loads(self.record_index_path.read_text())
            else:
                index = {"version": 1, "cluster_id": self.cluster_config.id, "records": {}}
            if index.get("cluster_id") != self.cluster_config.id or not isinstance(index.get("records"), dict):
                index = {"version": 1, "cluster_id": self.cluster_config.id, "records": {}}
            index["records"][record.record_id] = self._index_entry(record, path)
            index["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            atomic_write_text(self.record_index_path, json.dumps(index, indent=2, sort_keys=True) + "\n")
        except Exception:
            return

    def _index_entry(self, record: RecordEnvelope, path: Path) -> dict[str, Any]:
        return {
            "record_id": record.record_id,
            "path": str(path),
            "node_id": record.node_id,
            "node_seq": record.node_seq,
            "hlc": record.hlc,
            "kind": record.kind,
            "namespace": record.namespace,
            "key_id": record.data.get("encryption", {}).get("key_id"),
            "payload_hash": record.payload_hash,
        }

    def _update_imported_head(self, record: RecordEnvelope) -> None:
        current = self._read_head(record.node_id)
        if current is None:
            self._write_head(record.node_id, record.node_seq, record.record_id)
            return
        current_seq = current.get("seq", 0)
        current_record_id = current.get("record_id")
        if not isinstance(current_seq, int) or record.node_seq > current_seq:
            self._write_head(record.node_id, record.node_seq, record.record_id)
        elif record.node_seq == current_seq and current_record_id not in {None, record.record_id}:
            raise ValueError(f"Head conflict for {record.node_id} seq {record.node_seq}")

    def _next_hlc(self) -> HybridLogicalTimestamp:
        previous = self._load_clock()
        current = tick(previous, self.keypair.node_id)
        self._save_clock(current)
        return current

    def _merge_clock(self, remote_hlc: str) -> None:
        previous = self._load_clock()
        current = merge(previous, parse_hlc(remote_hlc), self.keypair.node_id)
        self._save_clock(current)

    def _load_clock(self) -> HybridLogicalTimestamp | None:
        path = self.cluster_dir / "clock.json"
        if not path.exists():
            return None
        try:
            return parse_hlc(json.loads(path.read_text())["hlc"])
        except Exception:
            return None

    def _save_clock(self, value: HybridLogicalTimestamp) -> None:
        atomic_write_text(self.cluster_dir / "clock.json", json.dumps({"hlc": str(value)}, indent=2) + "\n")

    def _public_key_for_record(self, record: RecordEnvelope) -> str:
        nodes = self.public_nodes()
        metadata = nodes.get(record.node_id)
        if metadata is not None:
            return metadata.signing_public_key_b64
        if record.kind != "node_membership":
            raise ValueError(f"Unknown node {record.node_id}")

        # Invite-backed bootstrap: decrypt membership with the cluster key,
        # verify an issuer already trusted by this store signed the invite,
        # then use the advertised new-node public key to verify the envelope.
        payload = decrypt_record_payload(record, secret=self.secret)
        invite = payload.get("invite")
        signing_public_key = payload.get("signing_public_key")
        if not isinstance(invite, dict) or not isinstance(signing_public_key, str):
            raise ValueError(f"Unknown node {record.node_id}")
        body = invite.get("body")
        signature = invite.get("signature")
        if not isinstance(body, dict) or not isinstance(signature, str):
            raise ValueError("Invalid invite metadata")
        issuer_id = body.get("issuer_node_id")
        issuer = nodes.get(issuer_id)
        if issuer is None:
            raise ValueError("Invite issuer is not trusted locally")
        from .crypto import verify_ed25519
        from .records import canonical_json_bytes

        if not verify_ed25519(issuer.signing_public_key_b64, canonical_json_bytes(body), signature):
            raise ValueError("Invite signature is invalid")
        expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc):
            raise ValueError("Invite has expired")
        if payload.get("node_id") != record.node_id:
            raise ValueError("Membership payload node_id mismatch")
        return signing_public_key

    def _apply_membership_record(self, record: RecordEnvelope, payload: dict[str, Any]) -> None:
        operation = payload.get("operation")
        if operation == "add":
            node_id = payload.get("node_id")
            signing_public_key = payload.get("signing_public_key")
            if not isinstance(node_id, str) or not isinstance(signing_public_key, str):
                raise ValueError("Invalid membership add payload")
            validate_node_id(node_id)
            approved_by = payload.get("approved_by_node_id")
            if record.node_id != node_id and approved_by != record.node_id:
                raise ValueError("Membership payload node_id mismatch")
            self.write_node_metadata(
                NodeMetadata(
                    node_id=node_id,
                    alias=str(payload.get("alias") or node_id),
                    signing_public_key_b64=signing_public_key,
                )
            )
        elif operation == "revoke":
            node_id = payload.get("node_id")
            if not isinstance(node_id, str):
                raise ValueError("Invalid membership revoke payload")
            validate_node_id(node_id)
            existing = self.public_nodes().get(node_id)
            if existing is None:
                return
            self.write_node_metadata(
                NodeMetadata(
                    node_id=existing.node_id,
                    alias=existing.alias,
                    signing_public_key_b64=existing.signing_public_key_b64,
                    revoked=True,
                    revoked_after_hlc=record.hlc,
                )
            )

    def _apply_key_rotation_payload(self, record: RecordEnvelope, payload: dict[str, Any]) -> None:
        key_id = payload.get("new_key_id")
        data_key = payload.get("data_key_b64")
        if isinstance(key_id, str) and isinstance(data_key, str):
            add_cluster_data_key(
                self.config,
                self.cluster_config.id,
                key_id,
                data_key,
                activate=True,
                active_key_hlc=record.hlc,
            )
            self._reload_secret()

    def _reject_if_revoked_future(self, record: RecordEnvelope) -> None:
        metadata = self.public_nodes().get(record.node_id)
        if metadata is None or not metadata.revoked or record.kind == "node_membership":
            return
        if metadata.revoked_after_hlc is None or record.hlc > metadata.revoked_after_hlc:
            raise ValueError(f"Node {record.node_id} is revoked")

    def _reload_secret(self) -> None:
        self.secret = load_cluster_secret(self.config, self.cluster_config.id)

    def _record_diagnostic(self, status: str, record_id: str | None, reason: str) -> None:
        self.cluster_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": status,
            "record_id": record_id,
            "reason": reason,
        }
        with self.diagnostics_path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


def public_node_metadata_from_keypair(cluster_config: ClusterConfig, keypair: NodeKeypair) -> NodeMetadata:
    return NodeMetadata(
        node_id=keypair.node_id,
        alias=cluster_config.node_alias,
        signing_public_key_b64=keypair.signing_public_key_b64,
    )


def new_data_key_b64() -> str:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    return b64url_encode(ChaCha20Poly1305.generate_key())
