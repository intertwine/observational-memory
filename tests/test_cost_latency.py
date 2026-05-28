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
        "OM_REFLECTOR_MAX_INPUT_TOKENS",
        "OM_REFLECTOR_OBSERVATION_CHUNK_RATIO",
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


def test_reflect_chunked_keeps_each_fold_under_input_budget(monkeypatch):
    from observational_memory.reflect import _CHARS_PER_TOKEN

    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        # Return a large running document so the fold context would grow without
        # the bound — exactly the O(chunks x size) case the cap must contain.
        return "# Reflections\n\n" + "U" * 60000

    monkeypatch.setattr(reflect, "compress", fake_compress)
    # Small input ceiling + large reflections cap forces a small chunk budget ->
    # multiple folds, and a 60k running doc must be trimmed to fit. system_prompt
    # is sized realistically.
    cfg = Config(reflector_context_max_chars=37000, reflector_max_input_tokens=12000)
    max_input_chars = int(cfg.reflector_max_input_tokens * _CHARS_PER_TOKEN)
    system_prompt = "S" * 4000
    # Several date sections, each comfortably under the derived chunk budget, that
    # together require more than one fold.
    sections = "".join(f"## 2026-05-{d:02d}\n\n" + ("- obs line\n" * 600) for d in range(10, 20))
    observations = "# Observations\n\n" + sections
    reflect._reflect_chunked(system_prompt, "R" * 60000, observations, cfg)

    assert len(captured) >= 2, "expected multiple folds"
    for sys_prompt, user_content in captured:
        # The whole call (system prompt + user content) must stay under budget.
        assert len(sys_prompt) + len(user_content) <= max_input_chars, (
            f"fold exceeded budget: {len(sys_prompt) + len(user_content)} > {max_input_chars}"
        )
    # The oversized running document was bounded on later folds.
    assert any("truncated to fit the reflector input budget" in uc for _, uc in captured)


def test_reflect_chunked_single_oversized_section_stays_under_budget(monkeypatch):
    from observational_memory.reflect import _CHARS_PER_TOKEN

    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        return "# Reflections\n\nout"

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config(reflector_max_input_tokens=12000)
    max_input_chars = int(cfg.reflector_max_input_tokens * _CHARS_PER_TOKEN)
    # A SINGLE date section whose body dwarfs the per-call budget. The chunker
    # must split within the day rather than emit one oversized fold (or a
    # spurious header-only chunk).
    observations = "# Observations\n\n## 2026-05-20\n\n" + ("- a line of observation\n" * 4000)
    reflect._reflect_chunked("S" * 200, "R" * 5000, observations, cfg)

    assert len(captured) >= 2, "oversized single section should split across folds"
    for sys_prompt, user_content in captured:
        assert len(sys_prompt) + len(user_content) <= max_input_chars, (
            f"fold exceeded budget: {len(sys_prompt) + len(user_content)} > {max_input_chars}"
        )
        # No header-only / empty observation chunk — each fold carries real content.
        assert "a line of observation" in user_content


def test_default_input_ceiling_does_not_clamp_configured_cap():
    # The settled #65 default raises OM_REFLECTOR_MAX_INPUT_TOKENS so the
    # configured OM_REFLECTOR_CONTEXT_MAX_CHARS (default 48000) actually binds
    # rather than being silently clamped by the per-call input ceiling.
    from observational_memory.reflect import _reflector_budgets

    cfg = Config()  # all defaults: 48000 cap, 45000 input tokens, 0.6 ratio
    system_prompt = "S" * 4312  # realistic reflector prompt size
    amem_section = "A" * 4000  # near-max auto-memory section
    reflections_cap, _chunk_budget = _reflector_budgets(system_prompt, amem_section, cfg)
    # The effective cap equals the configured cap — the input ceiling is no
    # longer the binding constraint for the current corpus shape.
    assert reflections_cap == cfg.reflector_context_max_chars == 48000


def test_diagnostic_surfaces_configured_and_effective_caps(monkeypatch, caplog):
    # When the configured reflections cap is HIGHER than the effective per-call
    # cap (a small input ceiling clamps it), the warning must report BOTH the
    # configured and effective caps plus the binding ceiling — not just blame
    # OM_REFLECTOR_CONTEXT_MAX_CHARS=<effective>, which the operator never set.
    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        return "# Reflections\n\n" + "U" * 60000

    monkeypatch.setattr(reflect, "compress", fake_compress)
    # Configured cap 48000, but a tiny input ceiling clamps the effective cap
    # well below it -> the two values differ and must both be surfaced.
    cfg = Config(reflector_context_max_chars=48000, reflector_max_input_tokens=12000)
    sections = "".join(f"## 2026-05-{d:02d}\n\n" + ("- obs line\n" * 600) for d in range(10, 20))
    observations = "# Observations\n\n" + sections
    with caplog.at_level("WARNING", logger="observational_memory.reflect"):
        reflect._reflect_chunked("S" * 4000, "R" * 60000, observations, cfg)

    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "configured_reflections_cap=48000" in warnings
    assert "effective_reflections_cap=" in warnings
    assert "configured_reflections_cap=48000 effective_reflections_cap=48000" not in warnings
    assert "max_input_tokens=12000" in warnings
    assert "observation_chunk_budget=" in warnings


def test_two_x_corpus_reflects_without_dropping_most_reflections(monkeypatch):
    # A ~2x-corpus scenario (reflections.md grown to ~96k, ~2x the 48k target)
    # must still reflect while keeping the bulk of the reflections context —
    # the raised default ceiling means the configured 48000 cap binds, so the
    # re-sent context is ~48k (most of a target doc), not the old ~12k clamp.
    captured: list[tuple[str, str]] = []

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured.append((system_prompt, user_content))
        return "# Reflections\n\n" + "R" * 96000

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config()  # defaults: 48000 cap, 45000 input tokens
    reflections = "# Reflections\n\n" + "R" * 96000  # ~2x the 48k target
    # Enough observations to force the chunked path (several large day sections).
    sections = "".join(f"## 2026-05-{d:02d}\n\n" + ("- obs line\n" * 5000) for d in range(10, 16))
    observations = "# Observations\n\n" + sections
    reflect._reflect_chunked("S" * 4312, reflections, observations, cfg)

    assert len(captured) >= 2, "expected the chunked path"
    # The reflections context re-sent on each fold keeps ~48k chars (the
    # configured cap), i.e. half of the 96k doc — NOT clamped down near 12k.
    reflections_blocks = [uc.split("---", 1)[0] for _, uc in captured]
    max_reflections_chars = max(len(b) for b in reflections_blocks)
    assert max_reflections_chars >= 40000, (
        f"reflections context dropped to {max_reflections_chars} chars; "
        "the raised input ceiling should keep ~48k, not the old ~12k clamp"
    )


def test_bound_reflections_context_keeps_head_and_marks_truncation():
    big = "HEAD" + "x" * 50_000
    out = _bound_reflections_context(big, 1000)
    assert len(out) <= 1000
    assert out.startswith("HEAD")
    assert "truncated to fit the reflector input budget" in out


def test_reflect_single_applies_bound(monkeypatch):
    captured = {}

    def fake_compress(system_prompt, user_content, config, **kwargs):
        captured["user"] = user_content
        return "# Reflections\n\nupdated"

    monkeypatch.setattr(reflect, "compress", fake_compress)
    cfg = Config(reflector_context_max_chars=1000)
    reflect._reflect_single("sys", "R" * 5000, "some observations", cfg)
    assert "truncated to fit the reflector input budget" in captured["user"]


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
