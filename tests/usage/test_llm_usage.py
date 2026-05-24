"""Integration tests: usage recording and budget enforcement through compress()."""

from __future__ import annotations

import pytest

from observational_memory import llm
from observational_memory.config import Config
from observational_memory.usage.budgets import BudgetExceededError
from observational_memory.usage.models import LLMUsage
from observational_memory.usage.tracker import UsageTracker


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for key in ("OM_LLM_MODEL", "OM_LLM_REFLECTOR_MODEL", "OM_BUDGET_BYPASS", "OM_BUDGET_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OM_USAGE_TRACKING", "1")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    monkeypatch.setenv("OM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")


def _summary(cfg):
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        return t.summary(conn)


def test_compress_records_provider_usage(cfg, monkeypatch):
    def fake(system_prompt, user_content, model, max_tokens, config):
        return "hello", LLMUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500, token_source="provider")

    monkeypatch.setattr(llm, "_call_anthropic_direct", fake)
    out = llm.compress("sys", "user", config=cfg, operation="reflector")
    assert out == "hello"
    summary = _summary(cfg)
    assert summary["ok_calls"] == 1
    assert summary["total_tokens"] == 1500
    # claude-sonnet-4-5: 1000/1M*3 + 500/1M*15 = 0.0105
    assert round(summary["total_usd"], 6) == 0.0105


def test_compress_tolerates_bare_string_return(cfg, monkeypatch):
    """Back-compat: a provider helper returning a bare str still records (estimated)."""

    def fake(system_prompt, user_content, model, max_tokens, config):
        return "ok"

    monkeypatch.setattr(llm, "_call_anthropic_direct", fake)
    out = llm.compress("sys", "user", config=cfg, operation="observer")
    assert out == "ok"
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        rows = t.tail(conn, limit=1)
    assert rows[0].token_source == "estimate"
    assert rows[0].status == "ok"


def test_hard_budget_blocks_and_records(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.0001")
    monkeypatch.setenv("OM_BUDGET_MODE", "hard")
    cfg2 = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)

    def fake(system_prompt, user_content, model, max_tokens, config):
        return "should not happen", None

    monkeypatch.setattr(llm, "_call_anthropic_direct", fake)
    with pytest.raises(BudgetExceededError):
        llm.compress("sys", "user", config=cfg2, operation="reflector")
    summary = _summary(cfg2)
    assert summary["blocked_calls"] == 1
    assert summary["ok_calls"] == 0


def test_budget_bypass_allows_call(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.0001")
    monkeypatch.setenv("OM_BUDGET_MODE", "hard")
    monkeypatch.setenv("OM_BUDGET_BYPASS", "1")
    cfg2 = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)

    def fake(system_prompt, user_content, model, max_tokens, config):
        return "done", None

    monkeypatch.setattr(llm, "_call_anthropic_direct", fake)
    assert llm.compress("sys", "user", config=cfg2, operation="reflector") == "done"


def test_tracking_off_creates_no_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_USAGE_TRACKING", "0")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    monkeypatch.setenv("OM_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")

    monkeypatch.setattr(llm, "_call_anthropic_direct", lambda *a, **k: ("x", None))
    llm.compress("sys", "user", config=cfg, operation="reflector")
    assert not cfg.usage_db_path.exists()


def test_responses_usage_extraction():
    from types import SimpleNamespace

    usage = SimpleNamespace(input_tokens=120, output_tokens=30, total_tokens=150)
    extracted = llm._responses_usage(usage)
    assert extracted is not None
    assert extracted.prompt_tokens == 120
    assert extracted.completion_tokens == 30
    assert extracted.total_tokens == 150
    assert extracted.token_source == "provider"
    assert llm._responses_usage(None) is None
