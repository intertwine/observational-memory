"""Tests for resolve_runtime_credentials (refresh + 401 retry semantics)."""

from __future__ import annotations

import base64
import json
import time

import pytest

from observational_memory.auth import runtime
from observational_memory.auth.errors import AuthError
from observational_memory.auth.store import (
    auth_store_lock,
    load_auth_store,
    load_provider_state,
    save_auth_store,
    save_provider_state,
)


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": "u"}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _seed_chatgpt(access_jwt: str, refresh: str = "RT") -> None:
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(
            store,
            "openai-chatgpt",
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": access_jwt, "refresh_token": refresh},
                "base_url": "https://chatgpt.com/backend-api/codex",
            },
        )
        save_auth_store(store)


def test_resolve_returns_existing_token_when_fresh(isolated_auth, monkeypatch) -> None:
    jwt = _make_jwt(int(time.time()) + 3600)
    _seed_chatgpt(jwt)

    def _no_refresh(*a, **k):
        raise AssertionError("refresh should not be called")

    monkeypatch.setattr(runtime._chatgpt, "refresh_tokens", _no_refresh)
    creds = runtime.resolve_runtime_credentials("openai-chatgpt")
    assert creds["access_token"] == jwt
    assert creds["auth_mode"] == "chatgpt"
    assert creds["base_url"].endswith("/codex")


def test_resolve_refreshes_when_expiring(isolated_auth, monkeypatch) -> None:
    expiring = _make_jwt(int(time.time()) + 30)
    _seed_chatgpt(expiring)
    new_token = _make_jwt(int(time.time()) + 3600)
    called = {"n": 0}

    def _fake_refresh(refresh_token, **_):
        called["n"] += 1
        return {
            "access_token": new_token,
            "refresh_token": "RT2",
            "expires_at": "future",
            "last_refresh": "now",
        }

    monkeypatch.setattr(runtime._chatgpt, "refresh_tokens", _fake_refresh)
    creds = runtime.resolve_runtime_credentials("openai-chatgpt")
    assert called["n"] == 1
    assert creds["access_token"] == new_token

    state = load_provider_state(load_auth_store(), "openai-chatgpt")
    assert state["tokens"]["access_token"] == new_token
    assert state["tokens"]["refresh_token"] == "RT2"


def test_resolve_raises_when_unauthenticated(isolated_auth) -> None:
    with pytest.raises(AuthError) as exc_info:
        runtime.resolve_runtime_credentials("openai-chatgpt")
    assert exc_info.value.relogin_required is True


def test_resolve_unknown_provider_raises(isolated_auth) -> None:
    with pytest.raises(AuthError):
        runtime.resolve_runtime_credentials("not-a-provider")


def test_force_refresh_calls_refresh_even_if_fresh(isolated_auth, monkeypatch) -> None:
    jwt = _make_jwt(int(time.time()) + 3600)
    _seed_chatgpt(jwt)
    new_token = _make_jwt(int(time.time()) + 7200)

    def _fake_refresh(refresh_token, **_):
        return {
            "access_token": new_token,
            "refresh_token": "RT_F",
            "expires_at": None,
            "last_refresh": "now",
        }

    monkeypatch.setattr(runtime._chatgpt, "refresh_tokens", _fake_refresh)
    creds = runtime.resolve_runtime_credentials("openai-chatgpt", force_refresh=True)
    assert creds["access_token"] == new_token
