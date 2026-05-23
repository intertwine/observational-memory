"""Read-only importers for sibling-CLI auth files.

om never writes to ``~/.codex/auth.json`` or ``~/.grok/auth.json`` — we only
opportunistically read them when the user opts in via ``om login --import``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .errors import AuthError
from .pkce import decode_jwt_claims


def _codex_auth_path() -> Path:
    home = os.getenv("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "auth.json"


def _grok_auth_path() -> Path:
    home = os.getenv("GROK_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".grok"
    return base / "auth.json"


def _expires_at_from_access_token(access_token: str) -> str | None:
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def read_codex_cli_tokens() -> dict | None:
    """Return Codex CLI tokens shaped for our store, or None if absent/unreadable."""
    path = _codex_auth_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not access or not refresh:
        return None
    return {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": access, "refresh_token": refresh},
        "expires_at": _expires_at_from_access_token(access),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": "https://chatgpt.com/backend-api/codex",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "source": "import-codex-cli",
    }


def read_grok_cli_tokens() -> dict | None:
    """Return Grok CLI tokens shaped for our store, or None if absent/unreadable."""
    path = _grok_auth_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    tokens_obj = payload.get("tokens")
    if not isinstance(tokens_obj, dict):
        # Some Grok CLI layouts inline the access_token at the top level.
        tokens_obj = {
            k: payload.get(k)
            for k in ("access_token", "refresh_token", "id_token", "token_type", "expires_in")
            if k in payload
        }
    access = str(tokens_obj.get("access_token") or "").strip()
    refresh = str(tokens_obj.get("refresh_token") or "").strip()
    if not access or not refresh:
        return None
    return {
        "auth_mode": "oidc",
        "tokens": {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": str(tokens_obj.get("id_token") or "").strip(),
            "token_type": str(tokens_obj.get("token_type") or "Bearer").strip() or "Bearer",
            "expires_in": tokens_obj.get("expires_in"),
        },
        "expires_at": _expires_at_from_access_token(access),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": "https://api.x.ai/v1",
        "oidc_issuer": "https://auth.x.ai",
        "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
        "source": "import-grok-cli",
    }


def detect_cli_imports() -> dict[str, Path]:
    """Return a {provider_id: path} dict for any sibling-CLI auth files present."""
    detected: dict[str, Path] = {}
    cp = _codex_auth_path()
    if cp.is_file():
        detected["openai-chatgpt"] = cp
    gp = _grok_auth_path()
    if gp.is_file():
        detected["xai-oauth"] = gp
    return detected


def import_provider(provider_id: str) -> dict:
    """Read sibling-CLI tokens for ``provider_id``. Raises AuthError on miss."""
    if provider_id == "openai-chatgpt":
        state = read_codex_cli_tokens()
        if state is None:
            raise AuthError(
                f"No Codex CLI tokens found at {_codex_auth_path()}.",
                provider="openai-chatgpt",
                code="codex_cli_import_missing",
            )
        return state
    if provider_id == "xai-oauth":
        state = read_grok_cli_tokens()
        if state is None:
            raise AuthError(
                f"No Grok CLI tokens found at {_grok_auth_path()}.",
                provider="xai-oauth",
                code="grok_cli_import_missing",
            )
        return state
    raise AuthError(f"Unknown provider for --import: {provider_id}", provider=provider_id)
