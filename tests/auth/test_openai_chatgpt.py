"""Tests for the ChatGPT (Codex device-code) auth flow."""

from __future__ import annotations

import base64
import json
import time

import pytest

from observational_memory.auth import openai_chatgpt
from observational_memory.auth.errors import AuthError


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp, "email": "a@b.c", "sub": "u1"}).encode()).decode().rstrip("=")
    )
    return f"{header}.{payload}.sig"


def test_access_token_is_expiring_skew(monkeypatch) -> None:
    fake_now = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: fake_now)
    not_expiring = _make_jwt(int(fake_now) + 3600)
    expiring_soon = _make_jwt(int(fake_now) + 30)
    expired = _make_jwt(int(fake_now) - 10)

    assert openai_chatgpt.access_token_is_expiring(not_expiring, skew_seconds=120) is False
    assert openai_chatgpt.access_token_is_expiring(expiring_soon, skew_seconds=120) is True
    assert openai_chatgpt.access_token_is_expiring(expired, skew_seconds=0) is True


def _stub_httpx_post(monkeypatch, *, status: int, body: dict | str) -> None:
    import httpx

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status

        def json(self):
            if isinstance(body, dict):
                return body
            raise ValueError("not json")

        @property
        def text(self):
            return body if isinstance(body, str) else json.dumps(body)

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)


def test_refresh_tokens_happy_path(isolated_auth, monkeypatch) -> None:
    new_jwt = _make_jwt(int(time.time()) + 3600)
    _stub_httpx_post(monkeypatch, status=200, body={"access_token": new_jwt, "refresh_token": "RT2"})
    update = openai_chatgpt.refresh_tokens("RT1")
    assert update["access_token"] == new_jwt
    assert update["refresh_token"] == "RT2"
    assert update["expires_at"] is not None


def test_refresh_tokens_marks_invalid_grant_as_relogin(isolated_auth, monkeypatch) -> None:
    _stub_httpx_post(
        monkeypatch,
        status=400,
        body={"error": {"code": "invalid_grant", "message": "bad refresh"}},
    )
    with pytest.raises(AuthError) as exc_info:
        openai_chatgpt.refresh_tokens("RT1")
    assert exc_info.value.code == "invalid_grant"
    assert exc_info.value.relogin_required is True


def test_refresh_tokens_handles_refresh_token_reused(isolated_auth, monkeypatch) -> None:
    _stub_httpx_post(
        monkeypatch,
        status=400,
        body={"error": {"code": "refresh_token_reused"}},
    )
    with pytest.raises(AuthError) as exc_info:
        openai_chatgpt.refresh_tokens("RT1")
    assert exc_info.value.code == "refresh_token_reused"
    assert exc_info.value.relogin_required is True
    assert "another client" in str(exc_info.value)


def test_refresh_tokens_missing_refresh_token_raises_relogin() -> None:
    with pytest.raises(AuthError) as exc_info:
        openai_chatgpt.refresh_tokens("")
    assert exc_info.value.relogin_required is True
    assert exc_info.value.code == "codex_auth_missing_refresh_token"


def test_is_terminal_refresh_error() -> None:
    assert openai_chatgpt.is_terminal_refresh_error(
        AuthError("x", provider="openai-chatgpt", code="invalid_grant", relogin_required=True)
    )
    assert not openai_chatgpt.is_terminal_refresh_error(
        AuthError("x", provider="openai-chatgpt", code="codex_refresh_failed", relogin_required=False)
    )
    assert not openai_chatgpt.is_terminal_refresh_error(RuntimeError("x"))


def _make_jwt_with_account(account_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"exp": 2000000000, "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def test_cloudflare_headers_always_include_originator() -> None:
    headers = openai_chatgpt.cloudflare_headers("not-a-jwt")
    assert headers["originator"] == "codex_cli_rs"
    assert headers["User-Agent"].startswith("codex_cli_rs/")
    # No account id extractable from a non-JWT — header omitted, not crashing.
    assert "ChatGPT-Account-ID" not in headers


def test_cloudflare_headers_extract_account_id() -> None:
    jwt = _make_jwt_with_account("acct_abc123")
    headers = openai_chatgpt.cloudflare_headers(jwt)
    assert headers["ChatGPT-Account-ID"] == "acct_abc123"
    assert headers["originator"] == "codex_cli_rs"


def test_cloudflare_headers_tolerate_empty_token() -> None:
    headers = openai_chatgpt.cloudflare_headers("")
    assert headers["originator"] == "codex_cli_rs"
    assert "ChatGPT-Account-ID" not in headers


def test_codex_base_url_pinning() -> None:
    from observational_memory.auth.openai_chatgpt import (
        CODEX_INFERENCE_BASE_URL,
        validate_inference_base_url,
    )

    # Accepted: chatgpt.com and subdomains.
    assert (
        validate_inference_base_url("https://chatgpt.com/backend-api/codex") == "https://chatgpt.com/backend-api/codex"
    )
    assert validate_inference_base_url("https://x.chatgpt.com/v1") == "https://x.chatgpt.com/v1"
    # Empty → fallback.
    assert validate_inference_base_url("") == CODEX_INFERENCE_BASE_URL
    # Rejected (exfiltration / cleartext / look-alike) → fallback, never the bad host.
    for bad in (
        "https://attacker.example/v1",
        "http://chatgpt.com/v1",
        "https://chatgpt.com.evil.example/v1",
        "not a url",
    ):
        assert validate_inference_base_url(bad) == CODEX_INFERENCE_BASE_URL
