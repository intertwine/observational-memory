"""Tests for the auth store (round-trip, perms, locking)."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from observational_memory.auth.store import (
    auth_file_path,
    auth_store_lock,
    delete_provider_state,
    load_auth_store,
    load_provider_state,
    redact_token,
    save_auth_store,
    save_provider_state,
)


def test_round_trip_persists_provider_state(isolated_auth: Path) -> None:
    store = load_auth_store()
    save_provider_state(
        store,
        "openai-chatgpt",
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "abc", "refresh_token": "def"},
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    save_auth_store(store)

    reloaded = load_auth_store()
    state = load_provider_state(reloaded, "openai-chatgpt")
    assert state is not None
    assert state["tokens"]["access_token"] == "abc"
    assert state["base_url"].endswith("/codex")
    assert reloaded["active_provider"] == "openai-chatgpt"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_save_writes_0600(isolated_auth: Path) -> None:
    store = load_auth_store()
    save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "x"}})
    path = save_auth_store(store)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_delete_provider_state(isolated_auth: Path) -> None:
    store = load_auth_store()
    save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "x"}})
    save_provider_state(store, "xai-oauth", {"tokens": {"access_token": "y"}})
    save_auth_store(store)

    store = load_auth_store()
    assert delete_provider_state(store, "openai-chatgpt")
    assert not delete_provider_state(store, "openai-chatgpt")
    save_auth_store(store)

    store = load_auth_store()
    assert load_provider_state(store, "openai-chatgpt") is None
    assert load_provider_state(store, "xai-oauth") is not None
    assert store["active_provider"] == "xai-oauth"


def test_redact_token() -> None:
    assert redact_token("supersecrettoken1234") == "****1234"
    assert redact_token("abc") == "****"
    assert redact_token(None) == "<missing>"
    assert redact_token("") == "<missing>"


def test_corrupt_file_recovers_to_empty(isolated_auth: Path) -> None:
    isolated_auth.write_text("{not json")
    store = load_auth_store()
    assert store == {"version": 1, "providers": {}}
    assert (isolated_auth.with_suffix(".json.corrupt")).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_lock_contention_blocks_concurrent_writers(isolated_auth: Path) -> None:
    """Two threads racing on the lock; the second waits then succeeds."""
    order: list[str] = []

    def writer(name: str, hold_seconds: float, delay: float) -> None:
        time.sleep(delay)
        with auth_store_lock(timeout_seconds=5.0):
            order.append(f"{name}:enter")
            time.sleep(hold_seconds)
            order.append(f"{name}:exit")

    t1 = threading.Thread(target=writer, args=("A", 0.3, 0.0))
    t2 = threading.Thread(target=writer, args=("B", 0.05, 0.05))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert order == ["A:enter", "A:exit", "B:enter", "B:exit"]


def test_auth_file_path_obeys_override(tmp_path, monkeypatch) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv("OM_AUTH_FILE", str(target))
    assert auth_file_path() == target


def test_seat_belt_blocks_real_user_path(monkeypatch) -> None:
    """When PYTEST_CURRENT_TEST is set, refuse to touch ~/.config/observational-memory/auth.json."""
    monkeypatch.delenv("OM_AUTH_FILE", raising=False)
    real_home = Path.home()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))  # type: ignore[arg-type]
    # The fixture would have set OM_AUTH_FILE; clear it and assert refusal.
    with pytest.raises(RuntimeError, match="Refusing to touch real user auth store"):
        # Direct path so the seat-belt test isn't influenced by user XDG.
        from observational_memory.config import Config

        cfg = Config()
        real = real_home / ".config" / "observational-memory" / "auth.json"
        # Re-point Config so resolved path matches the real one
        monkeypatch.setattr(cfg, "env_file", real.parent / "env")
        auth_file_path(cfg)


def test_json_layout_matches_plan(isolated_auth: Path) -> None:
    store = load_auth_store()
    save_provider_state(
        store,
        "xai-oauth",
        {
            "auth_mode": "oidc",
            "tokens": {"access_token": "AT", "refresh_token": "RT"},
            "base_url": "https://api.x.ai/v1",
            "oidc_issuer": "https://auth.x.ai",
            "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
            "redirect_uri": "http://127.0.0.1:56121/callback",
            "scopes": [
                "openid",
                "profile",
                "email",
                "offline_access",
                "grok-cli:access",
                "api:access",
            ],
            "discovery": {
                "authorization_endpoint": "https://auth.x.ai/authorize",
                "token_endpoint": "https://auth.x.ai/oauth/token",
            },
            "source": "loopback-pkce",
        },
    )
    save_auth_store(store)
    raw = json.loads(isolated_auth.read_text())
    assert raw["providers"]["xai-oauth"]["client_id"] == "b1a00492-073a-47ea-816f-4c329264a828"
    assert raw["providers"]["xai-oauth"]["scopes"][-1] == "api:access"
