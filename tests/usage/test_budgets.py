"""Tests for budget resolution and pre-call enforcement decisions."""

from __future__ import annotations

import pytest

from observational_memory.config import Config
from observational_memory.usage.budgets import (
    _parse_number,
    check_budget,
    resolve_budgets,
)
from observational_memory.usage.tracker import record_call


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_USAGE_TRACKING", "1")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    return Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5.00", 5.0), ("5_000_000", 5_000_000.0), ("1,000", 1000.0), ("$2.50", 2.5), ("", None), ("nope", None)],
)
def test_parse_number_tolerates_separators(raw, expected):
    assert _parse_number(raw) == expected


def test_resolve_budgets_reads_env(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "5.00")
    monkeypatch.setenv("OM_BUDGET_REFLECTOR_DAILY_TOKENS", "200_000")
    monkeypatch.setenv("OM_BUDGET_REFLECTOR_DAILY_TOKENS_MODE", "soft")
    budgets = {(b.scope, b.window, b.unit): b for b in resolve_budgets(cfg)}
    assert budgets[("global", "day", "usd")].limit == 5.0
    assert budgets[("global", "day", "usd")].mode == "hard"
    refl = budgets[("reflector", "day", "tokens")]
    assert refl.limit == 200_000
    assert refl.mode == "soft"


def test_check_budget_allows_under_threshold(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "5.00")
    decision = check_budget(cfg, operation="reflector", est_usd=0.10, est_tokens=1000)
    assert decision.action == "allow"


def test_check_budget_blocks_hard_cap(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.05")
    monkeypatch.setenv("OM_BUDGET_MODE", "hard")
    decision = check_budget(cfg, operation="reflector", est_usd=0.10, est_tokens=1000)
    assert decision.action == "block"
    assert decision.block_reason


def test_check_budget_soft_cap_warns_not_blocks(cfg, monkeypatch):
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.05")
    monkeypatch.setenv("OM_BUDGET_MODE", "soft")
    decision = check_budget(cfg, operation="reflector", est_usd=0.10, est_tokens=1000)
    assert decision.action == "warn"
    assert decision.warnings


def test_check_budget_warns_when_approaching(cfg, monkeypatch):
    # Spend 0.045 of a 0.05 daily cap (90%), then a tiny next call -> approaching warn.
    record_call(
        cfg,
        provider="anthropic",
        model="claude-sonnet-4-5",
        operation="reflector",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        est_input_usd=0.0,
        est_output_usd=0.045,
        est_total_usd=0.045,
        latency_ms=1,
        retries=0,
        status="ok",
        token_source="provider",
        pricing_source="builtin",
    )
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.05")
    decision = check_budget(cfg, operation="reflector", est_usd=0.001, est_tokens=10)
    assert decision.action == "warn"


def test_recall_operation_is_exempt(cfg, monkeypatch):
    # Per-operation budgets only target observer/reflector; recall carries none.
    monkeypatch.setenv("OM_BUDGET_REFLECTOR_DAILY_USD", "0.01")
    decision = check_budget(cfg, operation="recall", est_usd=10.0, est_tokens=1)
    assert decision.action == "allow"
