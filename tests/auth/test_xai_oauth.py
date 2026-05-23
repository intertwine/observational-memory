"""Tests for the xAI loopback PKCE flow + refresh."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from observational_memory.auth import xai_oauth
from observational_memory.auth.errors import AuthError
from observational_memory.auth.oauth_loopback import (
    XAI_OAUTH_REDIRECT_HOST,
    XAI_OAUTH_REDIRECT_PATH,
    XAI_OAUTH_REDIRECT_PORT,
    validate_loopback_redirect_uri,
)


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": "u1"}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def test_build_authorize_url_includes_required_params() -> None:
    url = xai_oauth.build_authorize_url(
        authorization_endpoint="https://auth.x.ai/authorize",
        redirect_uri="http://127.0.0.1:56121/callback",
        code_challenge="C",
        state="S",
        nonce="N",
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["response_type"] == ["code"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["plan"] == ["generic"]
    assert qs["referrer"] == ["observational-memory"]
    assert qs["state"] == ["S"]
    assert qs["nonce"] == ["N"]
    scopes = qs["scope"][0].split()
    assert "openid" in scopes
    assert "grok-cli:access" in scopes
    assert "api:access" in scopes


def test_validate_loopback_redirect_uri_accepts_127_with_port() -> None:
    host, port, path = validate_loopback_redirect_uri("http://127.0.0.1:56121/callback")
    assert host == "127.0.0.1"
    assert port == 56121
    assert path == "/callback"


@pytest.mark.parametrize(
    "uri",
    [
        "https://127.0.0.1:56121/callback",
        "http://localhost:56121/callback",
        "http://127.0.0.1/callback",
    ],
)
def test_validate_loopback_redirect_uri_rejects_others(uri: str) -> None:
    with pytest.raises(AuthError):
        validate_loopback_redirect_uri(uri)


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
    # exchange_code_for_tokens uses httpx.post directly.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())


def test_refresh_tokens_happy_path(monkeypatch) -> None:
    new_jwt = _make_jwt(int(time.time()) + 3600)
    _stub_httpx_post(
        monkeypatch,
        status=200,
        body={"access_token": new_jwt, "refresh_token": "RT2", "token_type": "Bearer"},
    )
    update = xai_oauth.refresh_tokens(
        "RT1",
        token_endpoint="https://auth.x.ai/oauth/token",
    )
    assert update["access_token"] == new_jwt
    assert update["refresh_token"] == "RT2"


def test_refresh_tokens_403_maps_to_tier_denied(monkeypatch) -> None:
    _stub_httpx_post(monkeypatch, status=403, body={"error": "forbidden"})
    with pytest.raises(AuthError) as exc_info:
        xai_oauth.refresh_tokens("RT1", token_endpoint="https://auth.x.ai/oauth/token")
    assert exc_info.value.code == "xai_oauth_tier_denied"
    assert exc_info.value.relogin_required is False
    assert "XAI_API_KEY" in str(exc_info.value) or "metered" in str(exc_info.value)


def test_refresh_tokens_400_marks_relogin(monkeypatch) -> None:
    _stub_httpx_post(monkeypatch, status=400, body={"error": "invalid_grant"})
    with pytest.raises(AuthError) as exc_info:
        xai_oauth.refresh_tokens("RT1", token_endpoint="https://auth.x.ai/oauth/token")
    assert exc_info.value.relogin_required is True


def test_refresh_tokens_missing_refresh_token() -> None:
    with pytest.raises(AuthError) as exc_info:
        xai_oauth.refresh_tokens("", token_endpoint="https://auth.x.ai/oauth/token")
    assert exc_info.value.code == "xai_auth_missing_refresh_token"
    assert exc_info.value.relogin_required is True


def test_refresh_tokens_rejects_non_xai_token_endpoint(monkeypatch) -> None:
    """Cached token_endpoint must be re-validated on the refresh hot path."""
    with pytest.raises(AuthError) as exc_info:
        xai_oauth.refresh_tokens("RT1", token_endpoint="https://attacker.example/token")
    assert exc_info.value.code == "xai_discovery_invalid"


def test_exchange_code_for_tokens_echoes_code_challenge(monkeypatch) -> None:
    """The xAI server #26990 quirk requires code_challenge at the token step."""
    import httpx

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": _make_jwt(int(time.time()) + 3600), "refresh_token": "R"}

        @property
        def text(self):
            return "ok"

    def _post(url, headers=None, data=None, timeout=None):
        captured["data"] = dict(data or {})
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)

    xai_oauth.exchange_code_for_tokens(
        token_endpoint="https://auth.x.ai/oauth/token",
        code="CODE",
        redirect_uri="http://127.0.0.1:56121/callback",
        code_verifier="V",
        code_challenge="C",
    )
    assert captured["data"]["code_challenge"] == "C"
    assert captured["data"]["code_challenge_method"] == "S256"
    assert captured["data"]["code_verifier"] == "V"


def test_exchange_code_for_tokens_403_tier_denied(monkeypatch) -> None:
    import httpx

    class _Resp:
        status_code = 403
        text = "forbidden"

        def json(self):
            return {"error": "forbidden"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(AuthError) as exc_info:
        xai_oauth.exchange_code_for_tokens(
            token_endpoint="https://auth.x.ai/oauth/token",
            code="C",
            redirect_uri="http://127.0.0.1:56121/callback",
            code_verifier="V",
            code_challenge="CC",
        )
    assert exc_info.value.code == "xai_oauth_tier_denied"


def test_access_token_is_expiring(monkeypatch) -> None:
    fake_now = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: fake_now)
    assert xai_oauth.access_token_is_expiring(_make_jwt(int(fake_now) + 30), skew_seconds=120) is True
    assert xai_oauth.access_token_is_expiring(_make_jwt(int(fake_now) + 9999), skew_seconds=120) is False
    assert xai_oauth.access_token_is_expiring("not-a-jwt") is False


def test_is_terminal_refresh_error() -> None:
    assert xai_oauth.is_terminal_refresh_error(
        AuthError("x", provider="xai-oauth", code="xai_refresh_failed", relogin_required=True)
    )
    assert not xai_oauth.is_terminal_refresh_error(
        AuthError("x", provider="xai-oauth", code="xai_oauth_tier_denied", relogin_required=False)
    )
    assert not xai_oauth.is_terminal_refresh_error(RuntimeError("x"))


_PORT_CONST_REQUIRED = (XAI_OAUTH_REDIRECT_HOST, XAI_OAUTH_REDIRECT_PORT, XAI_OAUTH_REDIRECT_PATH)
