"""Tests for remote-session detection + manual-paste callback parsing."""

from __future__ import annotations

from observational_memory.auth.remote import (
    is_remote_session,
    parse_pasted_callback,
)


def test_is_remote_session_detects_ssh(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 22 22")
    assert is_remote_session() is True


def test_is_remote_session_detects_codespaces(monkeypatch) -> None:
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("CODESPACES", "true")
    assert is_remote_session() is True


def test_is_remote_session_false_when_no_signals(monkeypatch) -> None:
    for v in (
        "SSH_CLIENT",
        "SSH_TTY",
        "CLOUD_SHELL",
        "CODESPACES",
        "CODESPACE_NAME",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "STACKBLITZ",
    ):
        monkeypatch.delenv(v, raising=False)
    assert is_remote_session() is False


def test_parse_pasted_callback_full_url() -> None:
    result = parse_pasted_callback("http://127.0.0.1:56121/callback?code=abc&state=xyz")
    assert result == {"code": "abc", "state": "xyz", "error": None, "error_description": None}


def test_parse_pasted_callback_query_fragment() -> None:
    result = parse_pasted_callback("?code=abc&state=xyz")
    assert result["code"] == "abc"
    assert result["state"] == "xyz"


def test_parse_pasted_callback_bare_kv() -> None:
    result = parse_pasted_callback("code=abc&state=xyz")
    assert result["code"] == "abc"
    assert result["state"] == "xyz"


def test_parse_pasted_callback_bare_code() -> None:
    result = parse_pasted_callback("just-a-code")
    assert result == {
        "code": "just-a-code",
        "state": None,
        "error": None,
        "error_description": None,
    }


def test_parse_pasted_callback_error() -> None:
    result = parse_pasted_callback("http://127.0.0.1:56121/callback?error=access_denied&error_description=user_denied")
    assert result["error"] == "access_denied"
    assert result["error_description"] == "user_denied"


def test_parse_pasted_callback_empty() -> None:
    assert parse_pasted_callback("") == {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }
