"""Tests for the OM Mail envelope wire format (sign, verify, encrypt, parse)."""

import json

import pytest

from observational_memory.mail.account import new_mail_keypair, new_shared_key_b64
from observational_memory.mail.envelope import (
    ENVELOPE_VERSION,
    EnvelopeError,
    MailEnvelope,
    create_envelope,
    decrypt_envelope_payload,
    parse_envelope,
    verify_envelope,
)

PAYLOAD = {"subject": "weekly sync", "markdown": "- remembered a thing"}


def _make_envelope(*, keys=None, shared_key=None, envelope_id=None, kind="memory-note"):
    private_b64, public_b64 = keys or new_mail_keypair()
    envelope = create_envelope(
        kind=kind,
        sender_address="alpha@example.com",
        sender_alias="alpha",
        signing_private_key_b64=private_b64,
        signing_public_key_b64=public_b64,
        payload=dict(PAYLOAD),
        shared_key_b64=shared_key,
        envelope_id=envelope_id,
    )
    return envelope, public_b64


class TestRoundTrip:
    def test_create_parse_round_trip(self):
        envelope, public_b64 = _make_envelope()
        parsed = parse_envelope(envelope.to_bytes())
        assert parsed.data == envelope.data
        assert parsed.kind == "memory-note"
        assert parsed.sender_address == "alpha@example.com"
        assert parsed.sender_alias == "alpha"
        assert parsed.sender_public_key_b64 == public_b64
        assert not parsed.payload_encrypted
        assert decrypt_envelope_payload(parsed, None) == PAYLOAD

    def test_verify_with_pinned_key(self):
        envelope, public_b64 = _make_envelope()
        assert verify_envelope(envelope, public_b64) is True

    def test_verify_with_wrong_key(self):
        envelope, _ = _make_envelope()
        _, other_public = new_mail_keypair()
        assert verify_envelope(envelope, other_public) is False


class TestTampering:
    @pytest.mark.parametrize(
        "field_name,new_value",
        [
            ("id", "omm_" + "f" * 32),
            ("kind", "context-pack"),
            ("sent_at", "2030-01-01T00:00:00Z"),
            ("payload", {"subject": "evil", "markdown": "tampered"}),
        ],
    )
    def test_tampered_field_breaks_verification(self, field_name, new_value):
        envelope, public_b64 = _make_envelope()
        tampered_data = dict(envelope.data)
        tampered_data[field_name] = new_value
        assert verify_envelope(MailEnvelope(data=tampered_data), public_b64) is False

    def test_tampered_sender_breaks_verification(self):
        envelope, public_b64 = _make_envelope()
        tampered_data = dict(envelope.data)
        tampered_data["sender"] = {**tampered_data["sender"], "address": "evil@example.com"}
        assert verify_envelope(MailEnvelope(data=tampered_data), public_b64) is False

    def test_embedded_key_must_match_pinned_key(self):
        # Sign with key A (envelope embeds A's public key) but pin key B: even
        # though the signature is valid under A, verification must fail.
        keys_a = new_mail_keypair()
        envelope, public_a = _make_envelope(keys=keys_a)
        assert verify_envelope(envelope, public_a) is True
        _, public_b = new_mail_keypair()
        assert verify_envelope(envelope, public_b) is False

    def test_embedded_key_matching_pin_with_foreign_signature_fails(self):
        # Envelope embeds the pinned key B but is signed with A's private key.
        private_a, _ = new_mail_keypair()
        _, public_b = new_mail_keypair()
        envelope, _ = _make_envelope(keys=(private_a, public_b))
        assert envelope.sender_public_key_b64 == public_b
        assert verify_envelope(envelope, public_b) is False


class TestEncryption:
    def test_encrypted_payload_round_trip(self):
        shared_key = new_shared_key_b64()
        envelope, public_b64 = _make_envelope(shared_key=shared_key)
        assert envelope.payload_encrypted
        assert "markdown" not in json.dumps(envelope.data["payload"])
        parsed = parse_envelope(envelope.to_bytes())
        assert verify_envelope(parsed, public_b64) is True
        assert decrypt_envelope_payload(parsed, shared_key) == PAYLOAD

    def test_decrypt_without_shared_key(self):
        envelope, _ = _make_envelope(shared_key=new_shared_key_b64())
        with pytest.raises(EnvelopeError, match="no shared key"):
            decrypt_envelope_payload(envelope, None)

    def test_decrypt_with_wrong_key(self):
        envelope, _ = _make_envelope(shared_key=new_shared_key_b64())
        with pytest.raises(EnvelopeError):
            decrypt_envelope_payload(envelope, new_shared_key_b64())

    def test_aad_binds_ciphertext_to_envelope_identity(self):
        # Splice envelope 1's EncryptedPayload into an envelope with a
        # different id: AAD binding must make decryption fail.
        shared_key = new_shared_key_b64()
        envelope, _ = _make_envelope(shared_key=shared_key, envelope_id="omm_" + "a" * 32)
        spliced_data = dict(envelope.data)
        spliced_data["id"] = "omm_" + "b" * 32
        spliced = MailEnvelope(data=spliced_data)
        with pytest.raises(EnvelopeError):
            decrypt_envelope_payload(spliced, shared_key)


class TestParseRejects:
    def _valid_data(self):
        envelope, _ = _make_envelope()
        return dict(envelope.data)

    def test_rejects_non_json(self):
        with pytest.raises(EnvelopeError, match="not valid JSON"):
            parse_envelope(b"\x80 not json at all")

    def test_rejects_non_dict(self):
        with pytest.raises(EnvelopeError, match="JSON object"):
            parse_envelope(json.dumps(["a", "list"]).encode())

    def test_rejects_wrong_version(self):
        data = self._valid_data()
        data["om_mail"] = ENVELOPE_VERSION + 1
        with pytest.raises(EnvelopeError, match="version"):
            parse_envelope(json.dumps(data).encode())

    def test_rejects_unknown_kind(self):
        data = self._valid_data()
        data["kind"] = "rootkit-install"
        with pytest.raises(EnvelopeError, match="kind"):
            parse_envelope(json.dumps(data).encode())

    def test_rejects_missing_sender(self):
        data = self._valid_data()
        del data["sender"]
        with pytest.raises(EnvelopeError, match="sender"):
            parse_envelope(json.dumps(data).encode())

    def test_rejects_missing_payload(self):
        data = self._valid_data()
        del data["payload"]
        with pytest.raises(EnvelopeError, match="payload"):
            parse_envelope(json.dumps(data).encode())

    def test_rejects_missing_signature(self):
        data = self._valid_data()
        del data["signature_b64"]
        with pytest.raises(EnvelopeError, match="signature_b64"):
            parse_envelope(json.dumps(data).encode())
