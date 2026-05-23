"""Host-pinning + base-URL exfiltration safeguards."""

from __future__ import annotations

import pytest

from observational_memory.auth.errors import AuthError
from observational_memory.auth.oidc_discovery import (
    validate_inference_base_url,
    validate_oauth_endpoint,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.x.ai/authorize",
        "https://accounts.x.ai/oauth/token",
        "https://something.deeper.x.ai/foo",
        "https://x.ai/oauth/token",
    ],
)
def test_endpoint_accepts_x_ai_origin(url: str) -> None:
    assert validate_oauth_endpoint(url, field="token_endpoint") == url


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/oauth/token",
        "http://auth.x.ai/oauth/token",  # non-HTTPS
        "https://auth.x.ai.evil.example/token",  # not actually a .x.ai subdomain
        "https://",  # no host
    ],
)
def test_endpoint_rejects_non_xai_or_insecure(url: str) -> None:
    with pytest.raises(AuthError) as exc_info:
        validate_oauth_endpoint(url, field="token_endpoint")
    assert exc_info.value.code == "xai_discovery_invalid"


def test_base_url_accepts_api_x_ai() -> None:
    assert validate_inference_base_url("https://api.x.ai/v1", fallback="X") == "https://api.x.ai/v1"
    assert validate_inference_base_url("", fallback="X") == "X"


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/v1",
        "http://api.x.ai/v1",  # non-HTTPS
        "https://api.x.ai.evil.example/v1",
        "not a url",
    ],
)
def test_base_url_rejects_non_xai_and_falls_back(url: str) -> None:
    """A malicious override must never replace the bearer destination."""
    fallback = "https://api.x.ai/v1"
    assert validate_inference_base_url(url, fallback=fallback) == fallback
