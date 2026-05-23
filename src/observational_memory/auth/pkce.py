"""RFC 7636 S256 PKCE helpers.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_oauth_pkce_code_verifier`` + ``_oauth_pkce_code_challenge``).
"""

from __future__ import annotations

import base64
import hashlib
import os


def code_verifier(length: int = 64) -> str:
    """Return a URL-safe S256 code verifier (43-128 chars)."""
    raw = base64.urlsafe_b64encode(os.urandom(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def code_challenge(verifier: str) -> str:
    """Return the S256 challenge for ``verifier`` per RFC 7636 §4.2."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def decode_jwt_claims(token: str) -> dict:
    """Best-effort JWT payload decode. Returns {} on any failure."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    import json

    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}
