"""Provider-routing regression tests for subscription stickiness.

Guards the v0.6.5 bug where `OM_LLM_MODEL=gpt-5.5` + `OM_LLM_PROVIDER=openai-chatgpt`
silently cross-routed to the metered `openai` provider via `_infer_provider`.
"""

from __future__ import annotations

import pytest

from observational_memory.llm import _infer_provider


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    for key in (
        "OM_LLM_PROVIDER",
        "OM_LLM_MODEL",
        "OM_LLM_OBSERVER_MODEL",
        "OM_LLM_REFLECTOR_MODEL",
        "OM_LLM_OBSERVER_PROVIDER",
        "OM_LLM_REFLECTOR_PROVIDER",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_openai_chatgpt_is_sticky_for_gpt_model(monkeypatch) -> None:
    # gpt-5.5 must NOT route away from the chosen ChatGPT subscription.
    assert _infer_provider("gpt-5.5", "openai-chatgpt") == "openai-chatgpt"
    assert _infer_provider("gpt-4o-mini", "openai-chatgpt") == "openai-chatgpt"


def test_xai_oauth_is_sticky_even_for_mismatched_model() -> None:
    # A non-grok model under an explicit xai-oauth choice stays on xai-oauth
    # (surfaces a clear provider-side error rather than a surprise metered bill).
    assert _infer_provider("gpt-5.5", "xai-oauth") == "xai-oauth"
    assert _infer_provider("grok-4.3", "xai-oauth") == "xai-oauth"


def test_metered_openai_still_redirects_claude_model() -> None:
    # Pre-existing behavior preserved for non-subscription defaults.
    assert _infer_provider("claude-sonnet-4-5", "openai") == "anthropic"


def test_grok_model_prefers_subscription_when_tokens_present(isolated_auth, monkeypatch) -> None:
    from observational_memory.auth.store import (
        auth_store_lock,
        load_auth_store,
        save_auth_store,
        save_provider_state,
    )

    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "xai-oauth", {"tokens": {"access_token": "T", "refresh_token": "R"}})
        save_auth_store(store)
    # Default provider is metered openai but the model is grok-* and tokens exist.
    assert _infer_provider("grok-4.3", "openai") == "xai-oauth"


def test_grok_model_falls_back_to_xai_api_key(isolated_auth, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert _infer_provider("grok-4.3", "openai") == "xai"


def test_codex_model_prefers_chatgpt_subscription(isolated_auth) -> None:
    from observational_memory.auth.store import (
        auth_store_lock,
        load_auth_store,
        save_auth_store,
        save_provider_state,
    )

    with auth_store_lock():
        store = load_auth_store()
        save_provider_state(store, "openai-chatgpt", {"tokens": {"access_token": "T", "refresh_token": "R"}})
        save_auth_store(store)
    assert _infer_provider("gpt-5-codex", "openai") == "openai-chatgpt"


# --- Per-workflow provider selection (OM_LLM_OBSERVER_PROVIDER / _REFLECTOR_) ---


def _cfg(**env):
    from observational_memory.config import Config

    return Config(**env)


def test_operation_provider_reads_overrides() -> None:
    cfg = _cfg(llm_observer_provider="xai-oauth", llm_reflector_provider="openai-chatgpt")
    assert cfg.operation_provider("observer") == "xai-oauth"
    assert cfg.operation_provider("reflector") == "openai-chatgpt"
    assert cfg.operation_provider(None) is None


def test_operation_provider_treats_auto_as_no_override() -> None:
    cfg = _cfg(llm_observer_provider="auto")
    assert cfg.operation_provider("observer") is None


def test_per_op_provider_ignores_global_model() -> None:
    # Global model is gpt-5.5 but observer is pinned to xai-oauth: the model must
    # resolve to the xai default, not the cross-provider global model.
    cfg = _cfg(llm_model="gpt-5.5", llm_observer_provider="xai-oauth")
    assert cfg.resolve_model("observer", "xai-oauth", ignore_global_model=True) == cfg.xai_oauth_model
    # Without the flag, the global model still wins (documents the difference).
    assert cfg.resolve_model("observer", "xai-oauth") == "gpt-5.5"


def test_xai_defaults_use_current_general_model(monkeypatch) -> None:
    monkeypatch.delenv("OM_XAI_OAUTH_MODEL", raising=False)
    monkeypatch.delenv("OM_XAI_MODEL", raising=False)
    cfg = _cfg()
    assert cfg.xai_oauth_model == "grok-4.3"
    assert cfg.xai_model == "grok-4.3"
    assert cfg.resolve_model(provider="xai-oauth") == "grok-4.3"
    assert cfg.resolve_model(provider="xai") == "grok-4.3"


def test_per_op_step_model_override_still_wins() -> None:
    cfg = _cfg(llm_observer_provider="xai-oauth", llm_observer_model="grok-2")
    assert cfg.resolve_model("observer", "xai-oauth", ignore_global_model=True) == "grok-2"


def test_compress_routes_per_operation(isolated_auth, monkeypatch) -> None:
    """observer pinned to anthropic, default openai → each op hits the right call."""
    import observational_memory.llm as llm_mod
    from observational_memory.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    calls: list[str] = []
    monkeypatch.setattr(llm_mod, "_call_anthropic_direct", lambda *a, **k: calls.append("anthropic") or "A")
    monkeypatch.setattr(llm_mod, "_call_openai_direct", lambda *a, **k: calls.append("openai") or "O")

    cfg = Config(llm_provider="openai", llm_observer_provider="anthropic", env_file=isolated_auth.parent / "env")
    llm_mod.compress("sys", "user", cfg, operation="observer")
    llm_mod.compress("sys", "user", cfg, operation="reflector")
    assert calls == ["anthropic", "openai"]


def test_infer_provider_uses_explicit_auth_file(tmp_path, monkeypatch) -> None:
    """A non-default auth store (config.auth_file) must be honored by inference."""
    import json

    from observational_memory.llm import _infer_provider

    # No OM_AUTH_FILE in env, no API keys, empty default XDG path — only an
    # explicit custom store path should be detected.
    monkeypatch.delenv("OM_AUTH_FILE", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "emptyconfig"))
    custom = tmp_path / "custom-auth.json"
    custom.write_text(
        json.dumps({"version": 1, "providers": {"xai-oauth": {"tokens": {"access_token": "T", "refresh_token": "R"}}}})
    )

    # Without the path, default location has no tokens → stays on default provider.
    assert _infer_provider("grok-4.3", "openai") == "openai"
    # With the explicit path, the grok model routes to the subscription.
    assert _infer_provider("grok-4.3", "openai", auth_file=custom) == "xai-oauth"
