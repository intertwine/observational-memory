"""Tests for LLM provider dispatch."""

import sys
from types import SimpleNamespace

import pytest

from observational_memory.config import Config
from observational_memory.llm import (
    _call_anthropic_direct,
    _call_openai_direct,
    _extract_anthropic_text,
    _parse_openai_chat_text,
    compress,
)


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
        def operation_provider(self, operation=None):
            return None

        def validate_provider_config(self, provider=None):
            return "unknown-provider"

        def resolve_model(self, operation=None, provider=None, ignore_global_model=False):
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


@pytest.mark.parametrize(
    ("model", "expected_token_arg"),
    [
        ("gpt-5.4", "max_completion_tokens"),
        ("gpt-5.2-chat-latest", "max_completion_tokens"),
        ("o4-mini", "max_completion_tokens"),
        ("gpt-4o-mini", "max_tokens"),
    ],
)
def test_openai_token_limit_parameter_matches_model_family(monkeypatch, model, expected_token_arg):
    request = {}

    class FakeCompletions:
        def create(self, **kwargs):
            request.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    text, _usage = _call_openai_direct("sys", "user", model, 8, Config())

    assert text == "ok"
    assert request[expected_token_arg] == 8
    unexpected_token_arg = "max_tokens" if expected_token_arg == "max_completion_tokens" else "max_completion_tokens"
    assert unexpected_token_arg not in request


def _block(block_type, **fields):
    """One Anthropic content block, SDK-object shaped."""
    return SimpleNamespace(type=block_type, **fields)


def _anthropic_message(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_anthropic_text_skips_leading_thinking_block():
    # Claude 5 shape: thinking display defaults to "omitted", so a non-trivial
    # answer leads with a thinking block whose text is empty.
    message = _anthropic_message(
        _block("thinking", text="", thinking=""),
        _block("text", text="- Works on the Insights portal"),
    )
    assert _extract_anthropic_text(message) == "- Works on the Insights portal"


def test_anthropic_text_skips_leading_redacted_thinking_block():
    message = _anthropic_message(
        _block("redacted_thinking", data="EnCrYpTeD"),
        _block("text", text="# Durable Observations"),
    )
    assert _extract_anthropic_text(message) == "# Durable Observations"


def test_anthropic_text_joins_multiple_text_blocks_in_order():
    # Citations can split one answer across several text blocks.
    message = _anthropic_message(
        _block("text", text="first half. "),
        _block("thinking", text=""),
        _block("text", text="second half."),
    )
    assert _extract_anthropic_text(message) == "first half. second half."


def test_anthropic_text_reads_a_plain_text_response():
    message = _anthropic_message(_block("text", text="ok"))
    assert _extract_anthropic_text(message) == "ok"


def test_anthropic_text_reads_dict_content_blocks():
    message = SimpleNamespace(
        content=[{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "ok"}],
        stop_reason="end_turn",
    )
    assert _extract_anthropic_text(message) == "ok"


def test_anthropic_text_without_any_text_names_the_block_types():
    message = _anthropic_message(
        _block("thinking", text=""),
        _block("redacted_thinking", data="EnCrYpTeD"),
        stop_reason="max_tokens",
    )
    with pytest.raises(RuntimeError) as excinfo:
        _extract_anthropic_text(message)
    error = str(excinfo.value)
    assert "thinking" in error
    assert "redacted_thinking" in error
    assert "max_tokens" in error


def test_anthropic_text_hint_is_omitted_when_max_tokens_is_not_the_cause():
    message = _anthropic_message(_block("text", text="   "), stop_reason="end_turn")
    with pytest.raises(RuntimeError) as excinfo:
        _extract_anthropic_text(message)
    assert "max_tokens" not in str(excinfo.value)


def test_anthropic_text_rejects_a_truncated_response():
    # Thinking tokens come out of the same max_tokens budget as the answer, so a
    # thinking-heavy turn can return a real but half-written text block.
    message = _anthropic_message(
        _block("thinking", text=""),
        _block("text", text="# Durable Observations\n- half a bul"),
        stop_reason="max_tokens",
    )
    with pytest.raises(RuntimeError, match="truncated"):
        _extract_anthropic_text(message)


def test_anthropic_text_excludes_non_text_blocks_that_carry_text():
    # Only `text` blocks hold assistant prose; anything else carrying a `text`
    # field (tool results, future block types) must not reach the memory file.
    message = _anthropic_message(
        _block("mcp_tool_result", text="internal tool payload"),
        _block("text", text="the answer"),
    )
    assert _extract_anthropic_text(message) == "the answer"


def test_anthropic_text_reads_already_flattened_string_content():
    assert _extract_anthropic_text(SimpleNamespace(content="ok", stop_reason="end_turn")) == "ok"


def test_anthropic_flattened_string_still_rejects_truncation():
    with pytest.raises(RuntimeError, match="truncated"):
        _extract_anthropic_text(SimpleNamespace(content="partial", stop_reason="max_tokens"))


def test_anthropic_text_without_content_blocks_raises():
    with pytest.raises(RuntimeError, match="no content blocks"):
        _extract_anthropic_text(SimpleNamespace(content=[]))


def test_anthropic_direct_call_reads_past_a_thinking_block(monkeypatch):
    class FakeMessages:
        def create(self, **_kwargs):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", text="", thinking=""),
                    SimpleNamespace(type="text", text="observation"),
                ],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=3, output_tokens=2),
            )

    class FakeAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))

    text, _usage = _call_anthropic_direct("sys", "user", "claude-sonnet-5", 100, Config())

    assert text == "observation"


def test_openai_chat_text_joins_content_parts():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=[
                        {"type": "reasoning", "summary": "..."},
                        {"type": "text", "text": "first half. "},
                        {"type": "text", "text": "second half."},
                    ]
                )
            )
        ]
    )
    assert _parse_openai_chat_text(response) == "first half. second half."


def test_openai_chat_text_without_text_parts_names_the_part_types():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=[{"type": "refusal", "refusal": "no"}]))]
    )
    with pytest.raises(RuntimeError, match="refusal"):
        _parse_openai_chat_text(response)


def test_openai_chat_text_without_choices_raises():
    with pytest.raises(RuntimeError, match="no choices"):
        _parse_openai_chat_text(SimpleNamespace(choices=[]))


def test_openai_chat_text_drops_reasoning_parts_that_carry_text():
    # Some OpenAI-compatible servers surface chain of thought as a content part
    # with its own `text` field. It must not be laundered into the memory file.
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=[
                        {"type": "reasoning", "text": "internal chain of thought "},
                        {"type": "text", "text": "the answer"},
                    ]
                )
            )
        ]
    )
    assert _parse_openai_chat_text(response) == "the answer"


@pytest.mark.parametrize(
    "content",
    [
        SimpleNamespace(text="hi"),  # a single part, not wrapped in a list
        {"type": "text", "text": "hi"},  # a bare dict content part
        7,
    ],
)
def test_openai_chat_text_reports_an_unexpected_content_shape(content):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    with pytest.raises(RuntimeError, match="did not include text content"):
        _parse_openai_chat_text(response)
