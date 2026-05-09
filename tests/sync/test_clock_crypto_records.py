from datetime import datetime, timedelta, timezone

import pytest

from observational_memory.sync.clock import merge, parse_hlc, tick
from observational_memory.sync.crypto import (
    decrypt_payload,
    encrypt_payload,
    generate_cluster_secret,
    generate_node_keypair,
    sign_ed25519,
    verify_ed25519,
)
from observational_memory.sync.records import (
    canonical_json_bytes,
    create_record,
    decrypt_record_payload,
    verify_record_envelope,
)


def test_canonical_json_is_stable():
    left = {"b": [2, 1], "a": {"z": "yes"}}
    right = {"a": {"z": "yes"}, "b": [2, 1]}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)


def test_hlc_strings_sort_and_monotonicity_survives_clock_skew():
    node = "node_test"
    start = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    first = tick(None, node, start)
    second = tick(first, node, start - timedelta(hours=1))
    remote = parse_hlc("2026-05-08T12:00:01.000000Z-000000-node_remote")
    third = merge(second, remote, node, start)

    assert str(first) < str(second) < str(third)
    assert second.counter == 1
    assert third.wall_time == remote.wall_time


def test_ed25519_and_chacha_roundtrip():
    keypair = generate_node_keypair("node-a")
    message = b"important"

    signature = sign_ed25519(keypair.signing_private_key_b64, message)
    assert verify_ed25519(keypair.signing_public_key_b64, message, signature)
    assert not verify_ed25519(keypair.signing_public_key_b64, b"changed", signature)

    secret = generate_cluster_secret()
    aad = b"metadata"
    encrypted = encrypt_payload(secret.data_key_b64, b"secret body", aad, key_id=secret.active_key_id)
    assert decrypt_payload(secret.data_key_b64, encrypted, aad) == b"secret body"


def test_record_verification_and_tamper_rejection():
    keypair = generate_node_keypair("node-a")
    secret = generate_cluster_secret()
    record = create_record(
        cluster_id=secret.cluster_id,
        keypair=keypair,
        secret=secret,
        kind="observation",
        namespace="personal",
        node_seq=1,
        hlc="2026-05-08T12:00:00.000000Z-000000-node_a",
        parents={},
        source={"agent": "codex"},
        payload={"format": "markdown", "body": "- secret memory"},
    )

    verify_record_envelope(record, cluster_id=secret.cluster_id, signing_public_key_b64=keypair.signing_public_key_b64)
    assert decrypt_record_payload(record, secret=secret)["body"] == "- secret memory"
    assert b"secret memory" not in record.to_bytes()

    tampered = record.data.copy()
    tampered["kind"] = "tombstone"
    with pytest.raises(ValueError, match="Record ID mismatch"):
        verify_record_envelope(
            type(record)(tampered),
            cluster_id=secret.cluster_id,
            signing_public_key_b64=keypair.signing_public_key_b64,
        )

    tampered_ciphertext = record.data.copy()
    replacement = "A" if tampered_ciphertext["payload_ciphertext"][0] != "A" else "B"
    tampered_ciphertext["payload_ciphertext"] = replacement + tampered_ciphertext["payload_ciphertext"][1:]
    with pytest.raises(ValueError):
        verify_record_envelope(
            type(record)(tampered_ciphertext),
            cluster_id=secret.cluster_id,
            signing_public_key_b64=keypair.signing_public_key_b64,
        )
