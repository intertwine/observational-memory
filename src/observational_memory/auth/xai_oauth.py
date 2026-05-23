"""xAI Grok OAuth (loopback authorization-code + PKCE) flow.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_xai_oauth_build_authorize_url``, ``_xai_oauth_exchange_code_for_tokens``,
``_xai_oauth_loopback_login``, ``refresh_xai_oauth_pure``,
``_xai_access_token_is_expiring``, ``_is_terminal_xai_oauth_refresh_error``).

Notable upstream quirks preserved here:
  * ``plan=generic`` + ``referrer=observational-memory`` authorize params —
    without ``plan=generic``, accounts.x.ai rejects loopback OAuth from
    non-allowlisted clients.
  * ``code_challenge`` echoed at the token step (#26990 — xAI's token
    endpoint re-validates the challenge rather than relying purely on
    server-side session state from the authorize step).
  * HTTP 403 from token / refresh endpoint → ``xai_oauth_tier_denied``
    rather than a generic refresh-failure; tells the user to use the
    metered ``xai`` provider instead.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from urllib.parse import urlencode

from .errors import AuthError
from .oauth_loopback import (
    XAI_OAUTH_REDIRECT_HOST,
    XAI_OAUTH_REDIRECT_PATH,
    XAI_OAUTH_REDIRECT_PORT,
    start_callback_server,
    validate_loopback_redirect_uri,
    wait_for_callback,
)
from .oidc_discovery import (
    XAI_OAUTH_ISSUER,
    fetch_xai_discovery,
    validate_inference_base_url,
    validate_oauth_endpoint,
)
from .pkce import code_challenge as _pkce_challenge
from .pkce import code_verifier as _pkce_verifier
from .remote import (
    is_remote_session,
    print_loopback_ssh_hint,
    prompt_manual_callback_paste,
)

XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
XAI_INFERENCE_BASE_URL_DEFAULT = "https://api.x.ai/v1"
XAI_OAUTH_DOCS_URL = "https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth"

_REFERRER = "observational-memory"


def _client_id() -> str:
    return (os.getenv("OM_XAI_OAUTH_CLIENT_ID") or XAI_OAUTH_CLIENT_ID).strip()


def _redirect_port() -> int:
    raw = (os.getenv("OM_XAI_OAUTH_REDIRECT_PORT") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return XAI_OAUTH_REDIRECT_PORT


def _timeout_seconds() -> float:
    raw = (os.getenv("OM_XAI_OAUTH_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 300.0


def access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    """True when the JWT access_token expires within ``skew_seconds``."""
    if not isinstance(access_token, str) or "." not in access_token:
        return False
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= (time.time() + max(0, int(skew_seconds)))
    except Exception:
        return False


def _expires_at_iso(access_token: str) -> str | None:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None
    return None


def build_authorize_url(
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    """Construct the xAI authorize URL with the upstream-required extras."""
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": _REFERRER,
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str,
    timeout_seconds: float = 20.0,
) -> dict:
    """Exchange the authorization code for tokens.

    Echoes ``code_challenge`` + ``code_challenge_method`` at the token
    step (defense-in-depth for the xAI #26990 quirk).
    """
    import httpx

    if not code_verifier:
        raise AuthError(
            "PKCE code_verifier is empty (bug in om).",
            provider="xai-oauth",
            code="xai_pkce_verifier_missing",
        )
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _client_id(),
        "code_verifier": code_verifier,
    }
    if code_challenge:
        data["code_challenge"] = code_challenge
        data["code_challenge_method"] = "S256"
    try:
        response = httpx.post(
            token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=data,
            timeout=max(20.0, timeout_seconds),
        )
    except Exception as exc:
        raise AuthError(
            f"xAI token exchange failed: {exc}",
            provider="xai-oauth",
            code="xai_token_exchange_failed",
        ) from exc
    if response.status_code != 200:
        body = (response.text or "").strip()
        if response.status_code == 403:
            raise AuthError(
                "xAI token exchange failed (HTTP 403)."
                + (f" Response: {body}" if body else "")
                + " This OAuth account is not authorized for xAI API access — "
                "set XAI_API_KEY and OM_LLM_PROVIDER=xai (metered path), or "
                "upgrade your subscription at https://x.ai/grok.",
                provider="xai-oauth",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise AuthError(
            f"xAI token exchange failed (HTTP {response.status_code})." + (f" Response: {body}" if body else ""),
            provider="xai-oauth",
            code="xai_token_exchange_failed",
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI token exchange returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI token exchange response was not a JSON object.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )
    return payload


def loopback_login(
    *,
    timeout_seconds: float | None = None,
    open_browser: bool = True,
    manual_paste: bool = False,
) -> dict:
    """Run the full xAI loopback authorization-code + PKCE flow.

    Returns a provider-state dict shaped for the ``xai-oauth`` slot in the
    auth store. Tokens are NOT persisted here — the caller writes them
    under the auth-store lock.
    """
    timeout = timeout_seconds if timeout_seconds is not None else _timeout_seconds()
    discovery = fetch_xai_discovery(timeout_seconds=timeout)
    authorization_endpoint = discovery["authorization_endpoint"]
    token_endpoint = discovery["token_endpoint"]

    if manual_paste:
        redirect_uri = f"http://{XAI_OAUTH_REDIRECT_HOST}:{_redirect_port()}{XAI_OAUTH_REDIRECT_PATH}"
        validate_loopback_redirect_uri(redirect_uri)
        verifier = _pkce_verifier()
        challenge = _pkce_challenge(verifier)
        state = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        authorize_url = build_authorize_url(
            authorization_endpoint=authorization_endpoint,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=state,
            nonce=nonce,
        )
        print("Open this URL to authorize om with xAI:")
        print(authorize_url)
        callback = prompt_manual_callback_paste(redirect_uri)
    else:
        server, thread, callback_result, redirect_uri = start_callback_server(preferred_port=_redirect_port())
        try:
            validate_loopback_redirect_uri(redirect_uri)
            verifier = _pkce_verifier()
            challenge = _pkce_challenge(verifier)
            state = uuid.uuid4().hex
            nonce = uuid.uuid4().hex
            authorize_url = build_authorize_url(
                authorization_endpoint=authorization_endpoint,
                redirect_uri=redirect_uri,
                code_challenge=challenge,
                state=state,
                nonce=nonce,
            )
            print("Open this URL to authorize om with xAI:")
            print(authorize_url)
            print()
            print(f"Waiting for callback on {redirect_uri}")
            print_loopback_ssh_hint(redirect_uri, docs_url=XAI_OAUTH_DOCS_URL)
            if open_browser and not is_remote_session():
                try:
                    opened = webbrowser.open(authorize_url)
                except Exception:
                    opened = False
                if opened:
                    print("Browser opened for xAI authorization.")
                else:
                    print("Could not open the browser automatically; use the URL above.")
            callback = wait_for_callback(
                server,
                thread,
                callback_result,
                timeout_seconds=max(30.0, timeout * 9),
            )
        except Exception:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
            raise

    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise AuthError(
            f"xAI authorization failed: {detail}",
            provider="xai-oauth",
            code="xai_authorization_failed",
        )
    if callback.get("state") != state:
        raise AuthError(
            "xAI authorization failed: state mismatch.",
            provider="xai-oauth",
            code="xai_state_mismatch",
        )
    code = str(callback.get("code") or "").strip()
    if not code:
        raise AuthError(
            "xAI authorization failed: missing authorization code.",
            provider="xai-oauth",
            code="xai_code_missing",
        )

    payload = exchange_code_for_tokens(
        token_endpoint=token_endpoint,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        code_challenge=challenge,
        timeout_seconds=timeout,
    )
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token:
        raise AuthError(
            "xAI token exchange did not return an access_token.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )
    if not refresh_token:
        raise AuthError(
            "xAI token exchange did not return a refresh_token.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )
    base_url = validate_inference_base_url(
        (os.getenv("OM_XAI_OAUTH_BASE_URL") or "").strip().rstrip("/")
        or (os.getenv("XAI_BASE_URL") or "").strip().rstrip("/"),
        fallback=XAI_INFERENCE_BASE_URL_DEFAULT,
    )
    return {
        "auth_mode": "oidc",
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(payload.get("id_token") or "").strip(),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
            "expires_in": payload.get("expires_in"),
        },
        "expires_at": _expires_at_iso(access_token),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": base_url,
        "oidc_issuer": XAI_OAUTH_ISSUER,
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "scopes": XAI_OAUTH_SCOPE.split(),
        "discovery": discovery,
        "source": "loopback-pkce",
    }


def refresh_tokens(
    refresh_token: str,
    *,
    token_endpoint: str = "",
    timeout_seconds: float = 20.0,
) -> dict:
    """Refresh xAI tokens. Returns a partial state update."""
    import httpx

    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "xAI OAuth is missing refresh_token.",
            provider="xai-oauth",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    endpoint = (token_endpoint or "").strip() or fetch_xai_discovery(timeout_seconds)["token_endpoint"]
    validate_oauth_endpoint(endpoint, field="token_endpoint")
    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        response = client.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": _client_id(),
                "refresh_token": refresh_token,
            },
        )
    if response.status_code != 200:
        detail = (response.text or "").strip()
        if response.status_code == 403:
            raise AuthError(
                "xAI token refresh failed with HTTP 403."
                + (f" Response: {detail}" if detail else "")
                + " This OAuth account is not authorized for xAI API access — "
                "set XAI_API_KEY and OM_LLM_PROVIDER=xai (metered path), or "
                "upgrade your subscription at https://x.ai/grok.",
                provider="xai-oauth",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise AuthError(
            "xAI token refresh failed." + (f" Response: {detail}" if detail else ""),
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=(response.status_code in {400, 401}),
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI token refresh returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_refresh_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI token refresh response was not a JSON object.",
            provider="xai-oauth",
            code="xai_refresh_invalid_response",
            relogin_required=True,
        )
    new_access = str(payload.get("access_token") or "").strip()
    if not new_access:
        raise AuthError(
            "xAI token refresh response was missing access_token.",
            provider="xai-oauth",
            code="xai_refresh_missing_access_token",
            relogin_required=True,
        )
    new_refresh = str(payload.get("refresh_token") or refresh_token).strip()
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "id_token": str(payload.get("id_token") or "").strip(),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        "expires_at": _expires_at_iso(new_access),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def is_terminal_refresh_error(exc: Exception) -> bool:
    return (
        isinstance(exc, AuthError)
        and exc.provider == "xai-oauth"
        and exc.code in {"xai_refresh_failed", "xai_auth_missing_refresh_token"}
        and bool(exc.relogin_required)
    )
