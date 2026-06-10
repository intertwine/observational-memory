"""OM Mail envelope v1 — the signed, optionally encrypted wire format.

The envelope travels as the ``om-mail.json`` email attachment (mail systems
rewrap and re-encode bodies; attachments stay byte-faithful). Signing and
encryption reuse the OM Cluster primitives in ``sync/crypto.py``:

- Ed25519 signature over the canonical JSON of the envelope minus
  ``signature_b64``. Verification is against the locally PINNED peer key —
  the key embedded in the envelope is informational only.
- Optional ChaCha20Poly1305 payload encryption under an out-of-band shared
  key, with AAD bound to the envelope identity so a ciphertext cannot be
  replayed under a different envelope.

Everything fails closed: malformed, unversioned, unknown-kind, tampered, or
undecryptable envelopes raise :class:`EnvelopeError`.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from observational_memory.sync.crypto import (
    EncryptedPayload,
    decrypt_payload,
    encrypt_payload,
    sign_ed25519,
    verify_ed25519,
)
from observational_memory.sync.records import canonical_json_bytes

ENVELOPE_VERSION = 1
ATTACHMENT_FILENAME = "om-mail.json"
SUBJECT_PREFIX = "[om-mail]"
SHARED_KEY_ID = "shared_v1"

KNOWN_MAIL_KINDS = frozenset(
    {
        "memory-note",
        "context-pack",
        "recall-request",
        "recall-response",
    }
)


class EnvelopeError(ValueError):
    """The envelope is malformed, unsupported, or fails verification."""


def new_mail_id() -> str:
    return "omm_" + secrets.token_hex(16)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class MailEnvelope:
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def kind(self) -> str:
        return str(self.data["kind"])

    @property
    def request_id(self) -> str | None:
        value = self.data.get("request_id")
        return str(value) if value is not None else None

    @property
    def sent_at(self) -> str:
        return str(self.data.get("sent_at", ""))

    @property
    def sender_address(self) -> str:
        return str(self.data.get("sender", {}).get("address", ""))

    @property
    def sender_alias(self) -> str | None:
        value = self.data.get("sender", {}).get("alias")
        return str(value) if value is not None else None

    @property
    def sender_public_key_b64(self) -> str:
        return str(self.data.get("sender", {}).get("signing_public_key_b64", ""))

    @property
    def payload_encrypted(self) -> bool:
        return bool(self.data.get("payload_encrypted", False))

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.data)


def envelope_aad(*, envelope_id: str, kind: str, sender_address: str) -> bytes:
    """Bind ciphertext to the envelope identity (anti-replay across envelopes)."""
    return canonical_json_bytes(
        {
            "id": envelope_id,
            "kind": kind,
            "om_mail": ENVELOPE_VERSION,
            "sender_address": sender_address,
        }
    )


def create_envelope(
    *,
    kind: str,
    sender_address: str,
    sender_alias: str | None,
    signing_private_key_b64: str,
    signing_public_key_b64: str,
    payload: dict[str, Any],
    request_id: str | None = None,
    shared_key_b64: str | None = None,
    envelope_id: str | None = None,
    sent_at: str | None = None,
) -> MailEnvelope:
    """Build and sign an envelope; encrypt the payload when a shared key is given."""
    if kind not in KNOWN_MAIL_KINDS:
        raise EnvelopeError(f"Unknown mail kind: {kind!r}")
    env_id = envelope_id or new_mail_id()
    if shared_key_b64:
        encrypted = encrypt_payload(
            shared_key_b64,
            canonical_json_bytes(payload),
            envelope_aad(envelope_id=env_id, kind=kind, sender_address=sender_address),
            key_id=SHARED_KEY_ID,
        )
        payload_field: dict[str, Any] = {"encrypted": encrypted.__dict__}
        payload_encrypted = True
    else:
        payload_field = payload
        payload_encrypted = False

    data: dict[str, Any] = {
        "om_mail": ENVELOPE_VERSION,
        "id": env_id,
        "kind": kind,
        "sent_at": sent_at or _utc_now_iso(),
        "sender": {
            "address": sender_address,
            "alias": sender_alias,
            "signing_public_key_b64": signing_public_key_b64,
        },
        "payload_encrypted": payload_encrypted,
        "payload": payload_field,
    }
    if request_id is not None:
        data["request_id"] = request_id
    data["signature_b64"] = sign_ed25519(signing_private_key_b64, canonical_json_bytes(data))
    return MailEnvelope(data=data)


def parse_envelope(raw: bytes) -> MailEnvelope:
    """Parse attachment bytes; reject anything that is not a well-formed v1 envelope."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"Envelope is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("Envelope must be a JSON object.")
    if data.get("om_mail") != ENVELOPE_VERSION:
        raise EnvelopeError(f"Unsupported envelope version: {data.get('om_mail')!r}")
    kind = data.get("kind")
    if kind not in KNOWN_MAIL_KINDS:
        raise EnvelopeError(f"Unknown mail kind: {kind!r}")
    for field_name in ("id", "sent_at", "signature_b64"):
        if not isinstance(data.get(field_name), str) or not data[field_name]:
            raise EnvelopeError(f"Envelope missing required field: {field_name}")
    sender = data.get("sender")
    if not isinstance(sender, dict) or not sender.get("address") or not sender.get("signing_public_key_b64"):
        raise EnvelopeError("Envelope missing sender identity.")
    if "payload" not in data:
        raise EnvelopeError("Envelope missing payload.")
    return MailEnvelope(data=data)


def verify_envelope(envelope: MailEnvelope, pinned_public_key_b64: str) -> bool:
    """Verify the signature against the locally pinned peer key (fail closed).

    The embedded sender key must equal the pinned key — a valid signature
    under a *different* key is still a verification failure, so a peer cannot
    silently rotate identity without the operator re-pinning.
    """
    if not pinned_public_key_b64:
        return False
    if envelope.sender_public_key_b64 != pinned_public_key_b64:
        return False
    unsigned = {k: v for k, v in envelope.data.items() if k != "signature_b64"}
    signature = envelope.data.get("signature_b64")
    if not isinstance(signature, str):
        return False
    try:
        return verify_ed25519(pinned_public_key_b64, canonical_json_bytes(unsigned), signature)
    except Exception:
        return False


def decrypt_envelope_payload(envelope: MailEnvelope, shared_key_b64: str | None) -> dict[str, Any]:
    """Return the plaintext payload dict, decrypting when needed (fail closed)."""
    payload = envelope.data.get("payload")
    if not envelope.payload_encrypted:
        if not isinstance(payload, dict):
            raise EnvelopeError("Envelope payload must be a JSON object.")
        return payload
    if not shared_key_b64:
        raise EnvelopeError("Payload is encrypted and no shared key is configured for this peer.")
    if not isinstance(payload, dict) or not isinstance(payload.get("encrypted"), dict):
        raise EnvelopeError("Encrypted envelope payload is malformed.")
    encrypted = payload["encrypted"]
    try:
        plaintext = decrypt_payload(
            shared_key_b64,
            EncryptedPayload(
                alg=str(encrypted.get("alg", "")),
                nonce=str(encrypted.get("nonce", "")),
                key_id=str(encrypted.get("key_id", "")),
                aad_hash=str(encrypted.get("aad_hash", "")),
                ciphertext=str(encrypted.get("ciphertext", "")),
            ),
            envelope_aad(
                envelope_id=envelope.id,
                kind=envelope.kind,
                sender_address=envelope.sender_address,
            ),
        )
        decoded = json.loads(plaintext.decode("utf-8"))
    except EnvelopeError:
        raise
    except Exception as exc:
        raise EnvelopeError(f"Failed to decrypt envelope payload: {exc}") from exc
    if not isinstance(decoded, dict):
        raise EnvelopeError("Decrypted payload must be a JSON object.")
    return decoded


def envelope_subject(kind: str, summary: str | None = None) -> str:
    suffix = f": {summary}" if summary else ""
    return f"{SUBJECT_PREFIX} {kind}{suffix}"
