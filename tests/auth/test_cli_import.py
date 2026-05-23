"""Tests for read-only importers from ~/.codex/ and ~/.grok/."""

from __future__ import annotations

import json

from observational_memory.auth.cli_import import (
    detect_cli_imports,
    read_codex_cli_tokens,
    read_grok_cli_tokens,
)


def test_codex_import_returns_none_when_absent(isolated_auth, monkeypatch) -> None:
    assert read_codex_cli_tokens() is None


def test_codex_import_reads_tokens(isolated_auth, tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "AT", "refresh_token": "RT"}}))
    state = read_codex_cli_tokens()
    assert state is not None
    assert state["tokens"]["access_token"] == "AT"
    assert state["tokens"]["refresh_token"] == "RT"
    assert state["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert state["auth_mode"] == "chatgpt"


def test_codex_import_skips_missing_fields(isolated_auth, tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "AT"}}))
    assert read_codex_cli_tokens() is None


def test_grok_import_reads_tokens(isolated_auth, tmp_path, monkeypatch) -> None:
    grok_home = tmp_path / "grok"
    grok_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    (grok_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "GT",
                    "refresh_token": "GR",
                    "token_type": "Bearer",
                }
            }
        )
    )
    state = read_grok_cli_tokens()
    assert state is not None
    assert state["tokens"]["access_token"] == "GT"
    assert state["base_url"] == "https://api.x.ai/v1"
    assert state["oidc_issuer"] == "https://auth.x.ai"


def test_detect_lists_both_when_present(isolated_auth, tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    grok_home = tmp_path / "grok"
    codex_home.mkdir(parents=True)
    grok_home.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a", "refresh_token": "b"}}))
    (grok_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "c", "refresh_token": "d"}}))
    detected = detect_cli_imports()
    assert set(detected) == {"openai-chatgpt", "xai-oauth"}
