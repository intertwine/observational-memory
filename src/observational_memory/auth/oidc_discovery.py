"""OIDC discovery fetch + host pinning for xAI endpoints.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_xai_oauth_discovery``, ``_xai_validate_oauth_endpoint``,
``_xai_validate_inference_base_url``).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from .errors import AuthError

logger = logging.getLogger(__name__)

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"


def validate_oauth_endpoint(url: str, *, field: str) -> str:
    """Reject any OIDC endpoint that isn't HTTPS on the xAI origin.

    The OIDC discovery output is cached in auth.json; a single MITM during
    initial login could substitute a malicious token_endpoint that would
    then receive every refresh_token in plaintext. Validating host + scheme
    on every read pins the cached endpoint to xAI's origin.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthError(
            f"xAI OIDC {field} must be HTTPS: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise AuthError(
            f"xAI OIDC {field} is missing a hostname: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise AuthError(
            f"xAI OIDC {field} host {host!r} is not on the xAI origin "
            "(expected x.ai or a *.x.ai subdomain). Refusing to use a "
            "cached endpoint that may have been substituted by a MITM.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    return url


def validate_inference_base_url(value: str, *, fallback: str) -> str:
    """Refuse a non-xAI base_url override; fall back instead of raising.

    The OAuth bearer is high-value; a tampered XAI_BASE_URL would ship it
    to a third party. Pin to ``api.x.ai`` / ``*.x.ai``. Warn-and-fallback
    rather than raising — a bad env var shouldn't deadlock auth, but it
    must never leak the bearer.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    try:
        parsed = urlparse(candidate)
    except Exception:
        logger.warning("Ignoring malformed xAI base_url override %r; using %s", candidate, fallback)
        return fallback
    if parsed.scheme != "https":
        logger.warning(
            "Refusing non-HTTPS xAI base_url override %r (bearer would be sent cleartext); using %s",
            candidate,
            fallback,
        )
        return fallback
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning("Ignoring xAI base_url override %r with no hostname; using %s", candidate, fallback)
        return fallback
    if host != "x.ai" and not host.endswith(".x.ai"):
        logger.warning(
            "Refusing xAI base_url override %r — host %r is not on the xAI origin; using %s",
            candidate,
            host,
            fallback,
        )
        return fallback
    return candidate


def fetch_xai_discovery(timeout_seconds: float = 15.0) -> dict[str, str]:
    """Fetch + validate the xAI OIDC discovery doc."""
    import httpx

    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery failed: {exc}",
            provider="xai-oauth",
            code="xai_discovery_failed",
        ) from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI OIDC discovery returned status {response.status_code}.",
            provider="xai-oauth",
            code="xai_discovery_failed",
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_discovery_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI OIDC discovery response was not a JSON object.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    authorize = str(payload.get("authorization_endpoint") or "").strip()
    token = str(payload.get("token_endpoint") or "").strip()
    if not authorize or not token:
        raise AuthError(
            "xAI OIDC discovery response missing required endpoints.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    validate_oauth_endpoint(authorize, field="authorization_endpoint")
    validate_oauth_endpoint(token, field="token_endpoint")
    return {"authorization_endpoint": authorize, "token_endpoint": token}
