"""OpenAI ChatGPT (Codex device-code) auth flow.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_codex_device_code_login``, ``refresh_codex_oauth_pure``,
``_codex_access_token_is_expiring``). Hermes-specific telemetry stripped;
endpoints and error handling preserved.

Endpoints (auth.openai.com):
  POST /api/accounts/deviceauth/usercode   (request)
  POST /api/accounts/deviceauth/token      (poll)
  POST /oauth/token                        (exchange + refresh)

Inference base URL: https://chatgpt.com/backend-api/codex
"""

from __future__ import annotations

import os
import time
import webbrowser
from datetime import datetime, timezone

from .errors import AuthError
from .pkce import decode_jwt_claims
from .remote import is_remote_session

CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/oauth/token"
CODEX_DEVICE_USERCODE_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/token"
CODEX_DEVICE_VERIFICATION_URL = f"{CODEX_OAUTH_ISSUER}/codex/device"
CODEX_INFERENCE_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


def access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    """True when ``access_token`` is within ``skew_seconds`` of expiry."""
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) <= (time.time() + max(0, int(skew_seconds)))


def cloudflare_headers(access_token: str) -> dict:
    """Headers required to avoid Cloudflare 403s on chatgpt.com/backend-api/codex.

    Ported from upstream Hermes (nousresearch/hermes-agent
    agent/auxiliary_client.py blob ~5fd3676, ``_codex_cloudflare_headers``).
    The Cloudflare layer in front of the Codex endpoint whitelists a small set
    of first-party originators (``codex_cli_rs``, ``codex_vscode``, …). Requests
    from non-residential IPs that don't advertise an allowed originator get a
    403 with ``cf-mitigated: challenge`` regardless of auth correctness. We pin
    ``originator: codex_cli_rs``, a codex_cli_rs-shaped ``User-Agent``, and the
    ``ChatGPT-Account-ID`` extracted from the OAuth JWT (``chatgpt_account_id``
    claim, canonical casing from codex-rs ``auth.rs``).

    Malformed tokens are tolerated — we drop the account-ID header rather than
    raise, so a bad token surfaces as a 401 instead of a construction crash.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Observational Memory)",
        "originator": "codex_cli_rs",
    }
    claims = decode_jwt_claims(access_token)
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        acct_id = auth_claim.get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    return headers


def _expires_at_iso(access_token: str) -> str | None:
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def device_code_login(*, client_id: str | None = None, open_browser: bool = True) -> dict:
    """Run the OpenAI device-code flow and return a provider-state dict.

    Returned shape matches the ``openai-chatgpt`` slot in the auth store.
    Tokens are NOT persisted here — the caller writes them under the lock.
    """
    import httpx

    cid = (client_id or os.getenv("OM_OPENAI_CHATGPT_CLIENT_ID") or CODEX_OAUTH_CLIENT_ID).strip()

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                CODEX_DEVICE_USERCODE_URL,
                json={"client_id": cid},
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        raise AuthError(
            f"Failed to request device code: {exc}",
            provider="openai-chatgpt",
            code="device_code_request_failed",
        ) from exc
    if resp.status_code != 200:
        raise AuthError(
            f"Device code request returned status {resp.status_code}.",
            provider="openai-chatgpt",
            code="device_code_request_error",
        )
    device_data = resp.json()
    user_code = device_data.get("user_code", "")
    device_auth_id = device_data.get("device_auth_id", "")
    poll_interval = max(3, int(device_data.get("interval") or 5))
    if not user_code or not device_auth_id:
        raise AuthError(
            "Device code response missing required fields.",
            provider="openai-chatgpt",
            code="device_code_incomplete",
        )

    print("To sign in, follow these steps:\n")
    print(f"  1. Open this URL in your browser:\n     {CODEX_DEVICE_VERIFICATION_URL}\n")
    print(f"  2. Enter this code:\n     {user_code}\n")
    print("Waiting for sign-in... (press Ctrl+C to cancel)")
    if open_browser and not is_remote_session():
        try:
            webbrowser.open(CODEX_DEVICE_VERIFICATION_URL)
        except Exception:
            pass

    code_resp: dict | None = None
    max_wait = 15 * 60
    start = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() - start < max_wait:
                time.sleep(poll_interval)
                poll = client.post(
                    CODEX_DEVICE_TOKEN_URL,
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue
                raise AuthError(
                    f"Device auth polling returned status {poll.status_code}.",
                    provider="openai-chatgpt",
                    code="device_code_poll_error",
                )
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        raise SystemExit(130)
    if code_resp is None:
        raise AuthError(
            "Login timed out after 15 minutes.",
            provider="openai-chatgpt",
            code="device_code_timeout",
        )

    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    redirect_uri = f"{CODEX_OAUTH_ISSUER}/deviceauth/callback"
    if not authorization_code or not code_verifier:
        raise AuthError(
            "Device auth response missing authorization_code or code_verifier.",
            provider="openai-chatgpt",
            code="device_code_incomplete_exchange",
        )
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": redirect_uri,
                    "client_id": cid,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        raise AuthError(
            f"Token exchange failed: {exc}",
            provider="openai-chatgpt",
            code="token_exchange_failed",
        ) from exc
    if token_resp.status_code != 200:
        raise AuthError(
            f"Token exchange returned status {token_resp.status_code}.",
            provider="openai-chatgpt",
            code="token_exchange_error",
        )
    tokens = token_resp.json()
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise AuthError(
            "Token exchange did not return an access_token.",
            provider="openai-chatgpt",
            code="token_exchange_no_access_token",
        )
    base_url = (os.getenv("OM_OPENAI_CHATGPT_BASE_URL") or "").strip().rstrip("/") or CODEX_INFERENCE_BASE_URL
    claims = decode_jwt_claims(access_token)
    return {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": access_token, "refresh_token": refresh_token},
        "id_token_claims": {
            "email": claims.get("email") or claims.get("preferred_username") or "",
            "sub": claims.get("sub") or "",
        },
        "expires_at": _expires_at_iso(access_token),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": base_url,
        "client_id": cid,
        "source": "device-code",
    }


def refresh_tokens(
    refresh_token: str,
    *,
    client_id: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict:
    """Refresh access_token via /oauth/token. Returns a partial state update."""
    import httpx

    cid = (client_id or os.getenv("OM_OPENAI_CHATGPT_CLIENT_ID") or CODEX_OAUTH_CLIENT_ID).strip()
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "ChatGPT auth is missing refresh_token.",
            provider="openai-chatgpt",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )
    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cid,
            },
        )
    if response.status_code != 200:
        code = "codex_refresh_failed"
        relogin_required = False
        message = f"ChatGPT token refresh failed with status {response.status_code}."
        try:
            err = response.json()
            if isinstance(err, dict):
                err_obj = err.get("error")
                if isinstance(err_obj, dict):
                    nested = err_obj.get("code") or err_obj.get("type")
                    if isinstance(nested, str) and nested.strip():
                        code = nested.strip()
                    msg = err_obj.get("message")
                    if isinstance(msg, str) and msg.strip():
                        message = f"ChatGPT token refresh failed: {msg.strip()}"
                elif isinstance(err_obj, str) and err_obj.strip():
                    code = err_obj.strip()
                    desc = err.get("error_description") or err.get("message")
                    if isinstance(desc, str) and desc.strip():
                        message = f"ChatGPT token refresh failed: {desc.strip()}"
        except Exception:
            pass
        if code in {"invalid_grant", "invalid_token", "invalid_request"}:
            relogin_required = True
        if code == "refresh_token_reused":
            message = (
                "ChatGPT refresh token was already consumed by another client "
                "(e.g. Codex CLI or VS Code extension). Re-run `om login openai-chatgpt`."
            )
            relogin_required = True
        if response.status_code in {401, 403} and not relogin_required:
            relogin_required = True
        raise AuthError(message, provider="openai-chatgpt", code=code, relogin_required=relogin_required)
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            "ChatGPT token refresh returned invalid JSON.",
            provider="openai-chatgpt",
            code="codex_refresh_invalid_json",
            relogin_required=True,
        ) from exc
    new_access = str(payload.get("access_token") or "").strip()
    if not new_access:
        raise AuthError(
            "ChatGPT token refresh response missing access_token.",
            provider="openai-chatgpt",
            code="codex_refresh_missing_access_token",
            relogin_required=True,
        )
    new_refresh = str(payload.get("refresh_token") or refresh_token).strip()
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_at": _expires_at_iso(new_access),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def is_terminal_refresh_error(exc: Exception) -> bool:
    return (
        isinstance(exc, AuthError)
        and exc.provider == "openai-chatgpt"
        and exc.code
        in {
            "codex_refresh_failed",
            "codex_auth_missing_refresh_token",
            "invalid_grant",
            "invalid_token",
            "refresh_token_reused",
        }
        and bool(exc.relogin_required)
    )
