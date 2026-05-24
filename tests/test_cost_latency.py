"""Tests for #52 cost/latency optimizations: reflect input bounding,
Codex reasoning-effort, and Anthropic prompt caching."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from observational_memory import reflect
from observational_memory.config import Config
from observational_memory.llm import _anthropic_system_blocks, _call_anthropic_direct, _call_codex_responses
from observational_memory.reflect import _bound_reflections_context


@pytest.fixture(autouse=True)
def _clear_effort_env(monkeypatch):
    for key in (
        "OM_OPENAI_CHATGPT_REASONING_EFFORT",
        "OM_OPENAI_CHATGPT_OBSERVER_REASONING_EFFORT",
        "OM_OPENAI_CHATGPT_REFLECTOR_REASONING_EFFORT",
        "OM_REFLECTOR_CONTEXT_MAX_CHARS",
    ):
        monkeypatch.delenv(key, raising=False)


# --- reflect input bounding ---


def test_bound_reflections_context_noop_when_under_cap():
    assert _bound_reflections_context("short doc", 32000) == "short doc"


def test_bound_reflections_context_disabled_with_zero():
    big = "x" * 50_000
    assert _bound_reflections_context(big, 0) == big


def test_bound_reflections_context_tiny_cap_never_exceeds():
    # Cap smaller than the marker: hard-truncate, never exceed, keep real content.
    out = _bound_reflections_context("HEADcontent" + "x" * 100, 10)
    assert len(out) <= 10
    assert out.startswith("HEAD")


def test_reflect_chunked_applies_bound(monkeypatch):
    captured: list[str] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append(user_content)
        # Return a large running document so the fold context would grow.
        return "# Reflections\n\n" + "U" * 6000

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config(reflector_context_max_chars=1000)
    # Two date sections force multiple chunks.
    observations = "# Observations\n\n" + "## 2026-05-20\n\n" + ("- a\n" * 400) + "## 2026-05-21\n\n" + ("- b\n" * 400)
    reflect._reflect_chunked("sys", "R" * 5000, observations, cfg)
    assert captured, "chunked reflect made no LLM calls"
    assert any("truncated to fit OM_REFLECTOR_CONTEXT_MAX_CHARS" in uc for uc in captured)


def test_bound_reflections_context_keeps_head_and_marks_truncation():
    big = "HEAD" + "x" * 50_000
    out = _bound_reflections_context(big, 1000)
    assert len(out) <= 1000
    assert out.startswith("HEAD")
    assert "truncated to fit OM_REFLECTOR_CONTEXT_MAX_CHARS" in out


def test_reflect_single_applies_bound(monkeypatch):
    captured = {}

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured["user"] = user_content
        return "# Reflections\n\nupdated"

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config(reflector_context_max_chars=1000)
    reflect._reflect_single("sys", "R" * 5000, "some observations", cfg)
    assert "truncated to fit OM_REFLECTOR_CONTEXT_MAX_CHARS" in captured["user"]


# --- Codex reasoning effort resolution ---


def test_reasoning_effort_defaults():
    cfg = Config()
    assert cfg.resolve_reasoning_effort("observer") == "low"
    assert cfg.resolve_reasoning_effort("reflector") is None
    assert cfg.resolve_reasoning_effort(None) is None


def test_reasoning_effort_global_override(monkeypatch):
    monkeypatch.setenv("OM_OPENAI_CHATGPT_REASONING_EFFORT", "high")
    cfg = Config()
    assert cfg.resolve_reasoning_effort("observer") == "high"
    assert cfg.resolve_reasoning_effort("reflector") == "high"


def test_reasoning_effort_per_operation_override_wins(monkeypatch):
    monkeypatch.setenv("OM_OPENAI_CHATGPT_REASONING_EFFORT", "high")
    monkeypatch.setenv("OM_OPENAI_CHATGPT_OBSERVER_REASONING_EFFORT", "low")
    cfg = Config()
    assert cfg.resolve_reasoning_effort("observer") == "low"
    assert cfg.resolve_reasoning_effort("reflector") == "high"


def test_reasoning_effort_invalid_value_is_ignored(monkeypatch):
    monkeypatch.setenv("OM_OPENAI_CHATGPT_REFLECTOR_REASONING_EFFORT", "bogus")
    cfg = Config()
    assert cfg.resolve_reasoning_effort("reflector") is None


# --- Codex Responses API forwards reasoning effort ---


def _fake_openai_with_capture(captured):
    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            done = SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    output_text="codex ok",
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                ),
            )
            return iter([done])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    return SimpleNamespace(OpenAI=FakeOpenAI)


def test_codex_forwards_reasoning_effort(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "openai", _fake_openai_with_capture(captured))
    monkeypatch.setattr("observational_memory.auth.openai_chatgpt.cloudflare_headers", lambda token: {})
    text, usage = _call_codex_responses(
        base_url="https://x",
        access_token="t",
        system_prompt="sys",
        user_content="u",
        model="gpt-5.5",
        max_tokens=100,
        reasoning_effort="low",
    )
    assert text == "codex ok"
    assert captured["reasoning"] == {"effort": "low"}
    assert usage is not None and usage.total_tokens == 15


def test_codex_omits_reasoning_when_unset(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "openai", _fake_openai_with_capture(captured))
    monkeypatch.setattr("observational_memory.auth.openai_chatgpt.cloudflare_headers", lambda token: {})
    _call_codex_responses(
        base_url="https://x",
        access_token="t",
        system_prompt="sys",
        user_content="u",
        model="gpt-5.5",
        max_tokens=100,
        reasoning_effort=None,
    )
    assert "reasoning" not in captured


# --- Anthropic prompt caching ---


def test_anthropic_system_blocks_carry_cache_control():
    blocks = _anthropic_system_blocks("the system prompt")
    assert blocks == [{"type": "text", "text": "the system prompt", "cache_control": {"type": "ephemeral"}}]


def test_anthropic_usage_folds_in_cache_tokens(monkeypatch):
    # With caching active, input_tokens excludes cached tokens; the cache
    # read/creation counts must be folded into the prompt total (else #51
    # accounting undercounts once caching is enabled).
    from observational_memory.llm import _anthropic_usage

    message = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=50,
            output_tokens=5,
        )
    )
    usage = _anthropic_usage(message)
    assert usage is not None
    assert usage.prompt_tokens == 160
    assert usage.completion_tokens == 5
    assert usage.total_tokens == 165


def test_compress_threads_reasoning_effort_to_codex(monkeypatch):
    monkeypatch.setenv("OM_USAGE_TRACKING", "0")
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai-chatgpt")
    monkeypatch.setattr("observational_memory.config._has_subscription_tokens", lambda *a, **k: True)
    captured: dict = {}

    def fake_chatgpt(system_prompt, user_content, model, max_tokens, config, reasoning_effort=None):
        captured["effort"] = reasoning_effort
        return "codex ok", None

    monkeypatch.setattr("observational_memory.llm._call_openai_chatgpt", fake_chatgpt)
    from observational_memory.llm import compress

    out = compress("sys", "user", config=Config(), operation="observer")
    assert out == "codex ok"
    assert captured["effort"] == "low"  # observer built-in default


def test_anthropic_call_sends_cacheable_system(monkeypatch):
    captured: dict = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok")],
                usage=SimpleNamespace(input_tokens=3, output_tokens=2),
            )

    class FakeAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    text, usage = _call_anthropic_direct("sys-prompt", "user", "claude-sonnet-4-5", 100, Config())
    assert text == "ok"
    system = captured["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "sys-prompt"
