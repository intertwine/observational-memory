"""Tests for PKCE + JWT helpers."""

from __future__ import annotations

import base64
import hashlib

from observational_memory.auth.pkce import (
    code_challenge,
    code_verifier,
    decode_jwt_claims,
)


def test_code_verifier_length_in_rfc_range() -> None:
    v = code_verifier()
    assert 43 <= len(v) <= 128
    # URL-safe charset, no padding
    assert "=" not in v


def test_code_challenge_matches_s256() -> None:
    v = "abc123"
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode("ascii").rstrip("=")
    assert code_challenge(v) == expected


def test_decode_jwt_claims_returns_payload() -> None:
    # exp = 2_000_000_000
    payload = base64.urlsafe_b64encode(b'{"exp":2000000000,"email":"a@b.c"}').decode().rstrip("=")
    jwt = f"header.{payload}.sig"
    claims = decode_jwt_claims(jwt)
    assert claims["exp"] == 2_000_000_000
    assert claims["email"] == "a@b.c"


def test_decode_jwt_claims_handles_garbage() -> None:
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims(123) == {}  # type: ignore[arg-type]
    assert decode_jwt_claims("a.b") == {}
