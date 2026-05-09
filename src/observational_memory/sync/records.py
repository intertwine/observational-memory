"""Signed encrypted OM Cluster record envelopes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .crypto import (
    ClusterSecret,
    EncryptedPayload,
    NodeKeypair,
    decrypt_payload,
    encrypt_payload,
    sha256_id,
    sign_ed25519,
    verify_ed25519,
)

KNOWN_RECORD_KINDS = {
    "observation",
    "reflection_snapshot",
    "manual_override",
    "tombstone",
    "node_membership",
    "key_rotation",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class RecordEnvelope:
    data: dict[str, Any]

    @property
    def record_id(self) -> str:
        return self.data["record_id"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def cluster_id(self) -> str:
        return self.data["cluster_id"]

    @property
    def node_id(self) -> str:
        return self.data["node_id"]

    @property
    def node_seq(self) -> int:
        return int(self.data["node_seq"])

    @property
    def hlc(self) -> str:
        return self.data["hlc"]

    @property
    def namespace(self) -> str:
        return self.data.get("namespace", "personal")

    @property
    def payload_hash(self) -> str:
        return self.data["payload_hash"]

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.data) + b"\n"

    @classmethod
    def from_bytes(cls, data: bytes) -> RecordEnvelope:
        return cls(json.loads(data.decode("utf-8")))


def create_record(
    *,
    cluster_id: str,
    keypair: NodeKeypair,
    secret: ClusterSecret,
    kind: str,
    namespace: str,
    node_seq: int,
    hlc: str,
    parents: dict[str, int],
    source: dict[str, Any],
    payload: dict[str, Any],
) -> RecordEnvelope:
    if kind not in KNOWN_RECORD_KINDS:
        raise ValueError(f"Unknown record kind: {kind}")
    plaintext = canonical_json_bytes(payload)
    payload_hash = sha256_id(plaintext)
    clear_metadata = {
        "cluster_id": cluster_id,
        "kind": kind,
        "namespace": namespace,
        "node_id": keypair.node_id,
        "node_seq": node_seq,
        "hlc": hlc,
        "parents": parents,
        "source": source,
    }
    aad = canonical_json_bytes(clear_metadata)
    encrypted = encrypt_payload(secret.data_key_b64, plaintext, aad, key_id=secret.active_key_id)
    unsigned = {
        "version": 1,
        **clear_metadata,
        "encryption": {
            "alg": encrypted.alg,
            "nonce": encrypted.nonce,
            "key_id": encrypted.key_id,
            "aad_hash": encrypted.aad_hash,
        },
        "payload_ciphertext": encrypted.ciphertext,
        "payload_hash": payload_hash,
    }
    record_id = sha256_id(canonical_json_bytes(unsigned))
    signable = {**unsigned, "record_id": record_id}
    signature = sign_ed25519(keypair.signing_private_key_b64, canonical_json_bytes(signable))
    return RecordEnvelope(
        {
            **signable,
            "signature": {
                "alg": "ed25519",
                "key_id": keypair.node_id,
                "sig": signature,
            },
        }
    )


def verify_record_envelope(
    record: RecordEnvelope,
    *,
    cluster_id: str,
    signing_public_key_b64: str,
) -> None:
    data = record.data
    if data.get("version") != 1:
        raise ValueError("Unsupported record version")
    if data.get("cluster_id") != cluster_id:
        raise ValueError("Record cluster_id mismatch")
    if data.get("kind") not in KNOWN_RECORD_KINDS:
        raise ValueError(f"Unknown record kind: {data.get('kind')}")
    recomputed = dict(data)
    signature = recomputed.pop("signature", None)
    if not isinstance(signature, dict) or signature.get("alg") != "ed25519":
        raise ValueError("Missing Ed25519 signature")
    existing_record_id = recomputed.pop("record_id", None)
    if existing_record_id != sha256_id(canonical_json_bytes(recomputed)):
        raise ValueError("Record ID mismatch")
    signable = {**recomputed, "record_id": existing_record_id}
    if not verify_ed25519(signing_public_key_b64, canonical_json_bytes(signable), signature.get("sig", "")):
        raise ValueError("Record signature verification failed")


def decrypt_record_payload(record: RecordEnvelope, *, secret: ClusterSecret) -> dict[str, Any]:
    encryption = record.data["encryption"]
    key_id = encryption.get("key_id", secret.active_key_id)
    data_key = secret.data_keys.get(key_id)
    if data_key is None:
        raise ValueError(f"Missing cluster data key {key_id}")
    encrypted = EncryptedPayload(
        alg=encryption["alg"],
        nonce=encryption["nonce"],
        key_id=key_id,
        aad_hash=encryption["aad_hash"],
        ciphertext=record.data["payload_ciphertext"],
    )
    aad = _aad_for_record(record)
    plaintext = decrypt_payload(data_key, encrypted, aad)
    if sha256_id(plaintext) != record.payload_hash:
        raise ValueError("Payload hash mismatch")
    return json.loads(plaintext.decode("utf-8"))


def record_path_name(record: RecordEnvelope) -> str:
    return f"{record.node_seq:020d}-{record.record_id}.omr.json"


def _aad_for_record(record: RecordEnvelope) -> bytes:
    return canonical_json_bytes(
        {
            "cluster_id": record.cluster_id,
            "kind": record.kind,
            "namespace": record.namespace,
            "node_id": record.node_id,
            "node_seq": record.node_seq,
            "hlc": record.hlc,
            "parents": record.data.get("parents", {}),
            "source": record.data.get("source", {}),
        }
    )
