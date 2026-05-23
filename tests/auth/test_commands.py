"""Tests for the om login / om logout / om auth ... CLI commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from observational_memory.auth.store import (
    auth_store_lock,
    load_auth_store,
    save_auth_store,
    save_provider_state,
)
from observational_memory.cli import cli


def test_login_help_lists_subscription_providers(isolated_auth) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["login", "--help"])
    assert result.exit_code == 0
    assert "openai-chatgpt" in result.output
    assert "xai-oauth" in result.output


def test_auth_status_empty(isolated_auth) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    assert "No providers configured" in result.output or "Active provider" in result.output


def test_auth_status_redacts_tokens(isolated_auth) -> None:
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(
            store,
            "openai-chatgpt",
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "supersecrettoken1234", "refresh_token": "RT"},
                "base_url": "https://chatgpt.com/backend-api/codex",
                "expires_at": "2026-12-01T00:00:00Z",
                "source": "device-code",
            },
        )
        save_auth_store(store)
    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "supersecrettoken" not in result.output
    assert "1234" in result.output  # last 4 still shown
    assert "openai-chatgpt" in result.output


def test_auth_status_json(isolated_auth) -> None:
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(
            store,
            "xai-oauth",
            {
                "auth_mode": "oidc",
                "tokens": {"access_token": "ABCDEFGH", "refresh_token": "R"},
                "base_url": "https://api.x.ai/v1",
            },
        )
        save_auth_store(store)
    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "status", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["active_provider"] == "xai-oauth"
    assert parsed["providers"][0]["access_token"].startswith("****")


def test_logout_removes_only_named_provider(isolated_auth) -> None:
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "a"}})
        save_provider_state(store, "xai-oauth", {"tokens": {"access_token": "b"}})
        save_auth_store(store)
    runner = CliRunner()
    result = runner.invoke(cli, ["logout", "openai-chatgpt"])
    assert result.exit_code == 0
    assert "openai-chatgpt" in result.output
    store = load_auth_store()
    assert "openai-chatgpt" not in store["providers"]
    assert "xai-oauth" in store["providers"]


def test_logout_all_when_no_provider(isolated_auth) -> None:
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "a"}})
        save_provider_state(store, "xai-oauth", {"tokens": {"access_token": "b"}})
        save_auth_store(store)
    runner = CliRunner()
    result = runner.invoke(cli, ["logout"])
    assert result.exit_code == 0
    store = load_auth_store()
    assert not store.get("providers")


def test_login_import_with_nothing(isolated_auth) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["login", "--import"])
    assert result.exit_code == 0
    assert "Nothing to import" in result.output


def test_login_import_from_codex(isolated_auth, tmp_path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "AT", "refresh_token": "RT"}}))
    runner = CliRunner()
    result = runner.invoke(cli, ["login", "--import"])
    assert result.exit_code == 0, result.output
    assert "Imported openai-chatgpt" in result.output
    store = load_auth_store()
    assert store["providers"]["openai-chatgpt"]["tokens"]["access_token"] == "AT"


def test_provider_summary_flags_footgun(isolated_auth, monkeypatch) -> None:
    """Subscription tokens present but a metered key wins under auto → warning."""
    from observational_memory.auth import provider_summary_lines
    from observational_memory.auth.store import (
        auth_store_lock,
        load_auth_store,
        save_auth_store,
        save_provider_state,
    )
    from observational_memory.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OM_LLM_PROVIDER", "auto")
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "abcd1234", "refresh_token": "r"}})
        save_auth_store(store)
    lines = "\n".join(provider_summary_lines(Config(env_file=isolated_auth.parent / "env")))
    assert "Resolved provider: anthropic" in lines
    assert "openai-chatgpt tokens are stored" in lines
    assert "API key wins" in lines


def test_provider_summary_no_footgun_when_provider_explicit(isolated_auth, monkeypatch) -> None:
    from observational_memory.auth import provider_summary_lines
    from observational_memory.auth.store import (
        auth_store_lock,
        load_auth_store,
        save_auth_store,
        save_provider_state,
    )
    from observational_memory.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai-chatgpt")
    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "abcd1234", "refresh_token": "r"}})
        save_auth_store(store)
    lines = "\n".join(provider_summary_lines(Config(env_file=isolated_auth.parent / "env")))
    assert "Resolved provider: openai-chatgpt" in lines
    assert "API key wins" not in lines


def test_login_sets_provider_in_env(isolated_auth, monkeypatch) -> None:
    """`om login openai-chatgpt` should pin OM_LLM_PROVIDER so the sub is used."""
    import observational_memory.auth.commands as cmds
    from observational_memory.config import Config

    monkeypatch.setattr(
        cmds._chatgpt,
        "device_code_login",
        lambda **k: {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "AT", "refresh_token": "RT"},
            "id_token_claims": {"email": "x@y.z"},
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    )
    cmds.login_openai_chatgpt(open_browser=False, set_default=True)
    assert "OM_LLM_PROVIDER=openai-chatgpt" in Config().env_file.read_text()


def test_login_no_set_default_skips_env(isolated_auth, monkeypatch) -> None:
    import observational_memory.auth.commands as cmds
    from observational_memory.config import Config

    monkeypatch.setattr(
        cmds._chatgpt,
        "device_code_login",
        lambda **k: {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "AT", "refresh_token": "RT"},
            "id_token_claims": {},
        },
    )
    cmds.login_openai_chatgpt(open_browser=False, set_default=False)
    env_file = Config().env_file
    assert not env_file.exists() or "OM_LLM_PROVIDER" not in env_file.read_text()
