"""Tests for LLM provider dispatch."""

import pytest

from observational_memory.config import Config
from observational_memory.llm import compress


@pytest.fixture(autouse=True)
def clear_llm_env(monkeypatch):
    for key in [
        "OM_LLM_PROVIDER",
        "OM_LLM_MODEL",
        "OM_LLM_OBSERVER_MODEL",
        "OM_LLM_REFLECTOR_MODEL",
        "OM_ANTHROPIC_MODEL",
        "OM_OPENAI_MODEL",
        "OM_VERTEX_PROJECT_ID",
        "OM_VERTEX_REGION",
        "OM_BEDROCK_REGION",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_REGION",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_dispatches_to_vertex_adapter(monkeypatch):
    config = Config(
        llm_provider="anthropic-vertex",
        vertex_project_id="proj",
        vertex_region="us-east5",
    )
    called = {}

    def fake_vertex(system_prompt, user_content, model, max_tokens, cfg):
        called["provider"] = "anthropic-vertex"
        called["model"] = model
        return "ok"

    monkeypatch.setattr("observational_memory.llm._call_anthropic_vertex", fake_vertex)
    result = compress("sys", "user", config=config, operation="observer")
    assert result == "ok"
    assert called["provider"] == "anthropic-vertex"
    assert called["model"] == "claude-sonnet-4-5-20250929"


def test_dispatches_to_bedrock_adapter(monkeypatch):
    config = Config(
        llm_provider="anthropic-bedrock",
        bedrock_region="us-east-1",
    )
    called = {}

    def fake_bedrock(system_prompt, user_content, model, max_tokens, cfg):
        called["provider"] = "anthropic-bedrock"
        called["model"] = model
        return "ok"

    monkeypatch.setattr("observational_memory.llm._call_anthropic_bedrock", fake_bedrock)
    result = compress("sys", "user", config=config, operation="reflector")
    assert result == "ok"
    assert called["provider"] == "anthropic-bedrock"
    assert called["model"] == "claude-sonnet-4-5-20250929"


def test_operation_specific_model_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = Config(
        llm_provider="anthropic",
        llm_model="shared-model",
        llm_observer_model="observer-model",
        llm_reflector_model="reflector-model",
    )
    calls = []

    def fake_anthropic(system_prompt, user_content, model, max_tokens, cfg):
        calls.append(model)
        return "ok"

    monkeypatch.setattr("observational_memory.llm._call_anthropic_direct", fake_anthropic)
    compress("sys", "user", config=config, operation="observer")
    compress("sys", "user", config=config, operation="reflector")
    assert calls == ["observer-model", "reflector-model"]


def test_unknown_provider_raises_value_error():
    class BadConfig:
        def validate_provider_config(self):
            return "unknown-provider"

        def resolve_model(self, operation=None, provider=None):
            return "model-x"

    try:
        compress("sys", "user", config=BadConfig())  # type: ignore[arg-type]
        assert False, "Should have raised"
    except ValueError as e:
        assert "unknown provider" in str(e).lower()


def test_adapter_errors_are_wrapped(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = Config(llm_provider="openai")

    def fake_openai(system_prompt, user_content, model, max_tokens, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr("observational_memory.llm._call_openai_direct", fake_openai)
    try:
        compress("sys", "user", config=config)
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "provider 'openai'" in str(e).lower()
