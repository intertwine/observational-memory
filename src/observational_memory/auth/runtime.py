"""Runtime credential resolver: reads + refreshes tokens for LLM calls.

Used from ``observational_memory.llm`` to convert a subscription-backed
provider id (``openai-chatgpt`` / ``xai-oauth``) into the
``(access_token, base_url)`` pair the OpenAI-compatible client needs.

Mirrors upstream Hermes ``resolve_codex_runtime_credentials`` /
``resolve_xai_oauth_runtime_credentials`` semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import openai_chatgpt as _chatgpt
from . import xai_oauth as _xai
from .errors import AuthError
from .store import (
    auth_store_lock,
    load_auth_store,
    load_provider_state,
    save_auth_store,
    save_provider_state,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_runtime_credentials(provider_id: str, *, force_refresh: bool = False) -> dict:
    """Return ``{"access_token", "base_url", "auth_mode"}`` for the given provider.

    Performs an in-place refresh under the auth-store lock when the cached
    access_token is expiring (per provider-specific skew). Persists refreshed
    tokens.
    """
    if provider_id == "openai-chatgpt":
        return _resolve_openai_chatgpt(force_refresh=force_refresh)
    if provider_id == "xai-oauth":
        return _resolve_xai_oauth(force_refresh=force_refresh)
    raise AuthError(
        f"Unknown subscription provider: {provider_id}",
        provider=provider_id,
        code="unknown_provider",
    )


def _resolve_openai_chatgpt(*, force_refresh: bool) -> dict:
    store = load_auth_store()
    state = load_provider_state(store, "openai-chatgpt")
    if not state:
        raise AuthError(
            "No ChatGPT tokens stored. Run `om login openai-chatgpt`.",
            provider="openai-chatgpt",
            code="codex_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    base_url = str(state.get("base_url") or _chatgpt.CODEX_INFERENCE_BASE_URL).strip()
    expiring = not access_token or _chatgpt.access_token_is_expiring(
        access_token, _chatgpt.CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    )
    if force_refresh or expiring:
        with auth_store_lock():
            store = load_auth_store()
            state = load_provider_state(store, "openai-chatgpt") or state
            tokens = dict(state.get("tokens") or {})
            access_token = str(tokens.get("access_token") or "").strip()
            refresh_token = str(tokens.get("refresh_token") or "").strip()
            recheck = (
                force_refresh
                or not access_token
                or _chatgpt.access_token_is_expiring(access_token, _chatgpt.CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
            )
            if recheck:
                update = _chatgpt.refresh_tokens(refresh_token)
                tokens["access_token"] = update["access_token"]
                tokens["refresh_token"] = update["refresh_token"]
                state["tokens"] = tokens
                state["expires_at"] = update["expires_at"]
                state["last_refresh"] = update["last_refresh"]
                save_provider_state(store, "openai-chatgpt", state, set_active=False)
                save_auth_store(store)
                access_token = update["access_token"]
    return {
        "access_token": access_token,
        "base_url": base_url,
        "auth_mode": "chatgpt",
    }


def _resolve_xai_oauth(*, force_refresh: bool) -> dict:
    store = load_auth_store()
    state = load_provider_state(store, "xai-oauth")
    if not state:
        raise AuthError(
            "No xAI tokens stored. Run `om login xai-oauth`.",
            provider="xai-oauth",
            code="xai_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens") or {}
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    discovery = state.get("discovery") or {}
    token_endpoint = str(discovery.get("token_endpoint") or "").strip()
    base_url = str(state.get("base_url") or _xai.XAI_INFERENCE_BASE_URL_DEFAULT).strip()
    expiring = not access_token or _xai.access_token_is_expiring(
        access_token, _xai.XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    )
    if force_refresh or expiring:
        with auth_store_lock():
            store = load_auth_store()
            state = load_provider_state(store, "xai-oauth") or state
            tokens = dict(state.get("tokens") or {})
            access_token = str(tokens.get("access_token") or "").strip()
            refresh_token = str(tokens.get("refresh_token") or "").strip()
            discovery = state.get("discovery") or {}
            token_endpoint = str(discovery.get("token_endpoint") or "").strip()
            recheck = (
                force_refresh
                or not access_token
                or _xai.access_token_is_expiring(access_token, _xai.XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
            )
            if recheck:
                update = _xai.refresh_tokens(refresh_token, token_endpoint=token_endpoint)
                tokens["access_token"] = update["access_token"]
                tokens["refresh_token"] = update["refresh_token"]
                if update.get("id_token"):
                    tokens["id_token"] = update["id_token"]
                tokens["token_type"] = update["token_type"]
                state["tokens"] = tokens
                state["expires_at"] = update["expires_at"]
                state["last_refresh"] = update["last_refresh"]
                save_provider_state(store, "xai-oauth", state, set_active=False)
                save_auth_store(store)
                access_token = update["access_token"]
    return {
        "access_token": access_token,
        "base_url": base_url,
        "auth_mode": "oidc",
    }
