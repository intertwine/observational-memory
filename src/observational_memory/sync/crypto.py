"""Cryptographic helpers for OM Cluster records."""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def sha256_id(data: bytes) -> str:
    return "sha256_" + sha256(data).hexdigest()


@dataclass(frozen=True)
class NodeKeypair:
    node_id: str
    signing_private_key_b64: str
    signing_public_key_b64: str
    alias: str | None = None


@dataclass(frozen=True)
class ClusterSecret:
    cluster_id: str
    data_keys: dict[str, str]
    active_key_id: str
    active_key_hlc: str | None = None

    @property
    def data_key_b64(self) -> str:
        return self.data_keys[self.active_key_id]

    @classmethod
    def single(cls, cluster_id: str, data_key_b64: str, key_id: str = "key_1") -> ClusterSecret:
        return cls(cluster_id=cluster_id, data_keys={key_id: data_key_b64}, active_key_id=key_id)


@dataclass(frozen=True)
class EncryptedPayload:
    alg: str
    nonce: str
    key_id: str
    aad_hash: str
    ciphertext: str


def generate_node_keypair(alias: str | None = None) -> NodeKeypair:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    node_id = "node_" + sha256(public_raw).hexdigest()[:24]
    return NodeKeypair(
        node_id=node_id,
        signing_private_key_b64=b64url_encode(private_raw),
        signing_public_key_b64=b64url_encode(public_raw),
        alias=alias,
    )


def generate_cluster_secret() -> ClusterSecret:
    cluster_id = "omc_" + secrets.token_hex(16)
    return ClusterSecret.single(cluster_id=cluster_id, data_key_b64=b64url_encode(ChaCha20Poly1305.generate_key()))


def sign_ed25519(private_key_b64: str, data: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(b64url_decode(private_key_b64))
    return b64url_encode(private_key.sign(data))


def verify_ed25519(public_key_b64: str, data: bytes, signature_b64: str) -> bool:
    public_key = Ed25519PublicKey.from_public_bytes(b64url_decode(public_key_b64))
    try:
        public_key.verify(b64url_decode(signature_b64), data)
    except InvalidSignature:
        return False
    return True


def encrypt_payload(data_key_b64: str, plaintext: bytes, aad: bytes, *, key_id: str) -> EncryptedPayload:
    nonce = secrets.token_bytes(12)
    cipher = ChaCha20Poly1305(b64url_decode(data_key_b64))
    ciphertext = cipher.encrypt(nonce, plaintext, aad)
    return EncryptedPayload(
        alg="chacha20poly1305",
        nonce=b64url_encode(nonce),
        key_id=key_id,
        aad_hash=sha256_id(aad),
        ciphertext=b64url_encode(ciphertext),
    )


def decrypt_payload(data_key_b64: str, encrypted: EncryptedPayload, aad: bytes) -> bytes:
    if encrypted.alg != "chacha20poly1305":
        raise ValueError(f"Unsupported encryption algorithm: {encrypted.alg}")
    if encrypted.aad_hash != sha256_id(aad):
        raise ValueError("Encryption AAD hash mismatch")
    cipher = ChaCha20Poly1305(b64url_decode(data_key_b64))
    return cipher.decrypt(b64url_decode(encrypted.nonce), b64url_decode(encrypted.ciphertext), aad)
