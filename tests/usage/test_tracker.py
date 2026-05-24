"""Tests for the SQLite usage tracker."""

from __future__ import annotations

import pytest

from observational_memory.config import Config
from observational_memory.usage import tracker
from observational_memory.usage.tracker import UsageTracker, record_call


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_USAGE_TRACKING", "1")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    return Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")


def _record(cfg, *, operation="reflector", usd=0.01, tokens=1500, status="ok"):
    return record_call(
        cfg,
        provider="anthropic",
        model="claude-sonnet-4-5",
        operation=operation,
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=tokens,
        est_input_usd=usd / 2,
        est_output_usd=usd / 2,
        est_total_usd=usd,
        latency_ms=123,
        retries=0,
        status=status,
        token_source="provider",
        pricing_source="builtin",
    )


def test_record_call_persists_row(cfg):
    row_id = _record(cfg)
    assert row_id > 0
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        summary = t.summary(conn)
    assert summary["calls"] == 1
    assert summary["total_tokens"] == 1500
    assert summary["total_usd"] == 0.01


def test_tracking_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_USAGE_TRACKING", "0")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    cfg = Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")
    assert _record(cfg) == 0
    assert not cfg.usage_db_path.exists()


def test_window_totals_only_counts_ok_rows(cfg):
    _record(cfg, usd=0.02, tokens=2000, status="ok")
    _record(cfg, usd=0.50, tokens=9999, status="blocked_by_budget")
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        usd, toks = t.window_totals(conn, since_utc=tracker.day_start_utc_iso())
    assert usd == 0.02
    assert toks == 2000


def test_summary_status_and_tail_agree(cfg):
    _record(cfg, usd=0.03, tokens=1000)
    _record(cfg, usd=0.07, tokens=2000)
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        summary = t.summary(conn)
        rows = t.tail(conn, limit=10)
    assert summary["calls"] == 2
    assert round(summary["total_usd"], 6) == 0.10
    assert round(sum(r.est_total_usd for r in rows), 6) == 0.10


def test_blocked_rows_excluded_from_cost_and_token_totals(cfg):
    # A blocked row carries a pre-call estimate that was never spent; it must
    # count toward call counts but NOT toward cost/token totals (matches tail).
    _record(cfg, usd=0.02, tokens=2000, status="ok")
    _record(cfg, usd=0.42, tokens=99999, status="blocked_by_budget")
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        summary = t.summary(conn)
    assert summary["calls"] == 2
    assert summary["ok_calls"] == 1
    assert summary["blocked_calls"] == 1
    assert summary["total_usd"] == 0.02
    assert summary["total_tokens"] == 2000
    # by_operation cost/tokens are also ok-only.
    refl = next(op for op in summary["by_operation"] if op["operation"] == "reflector")
    assert refl["total_usd"] == 0.02
    assert refl["total_tokens"] == 2000


def test_unknown_priced_ok_calls_are_counted_not_zeroed(cfg):
    # An ok call with no price (est_total_usd=NULL) must surface as unpriced,
    # not silently fold into a $0.00 total.
    _record(cfg, usd=0.02, tokens=1000, status="ok")
    record_call(
        cfg,
        provider="openai",
        model="some-unpriced-model",
        operation="reflector",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        est_input_usd=None,
        est_output_usd=None,
        est_total_usd=None,
        latency_ms=10,
        retries=0,
        status="ok",
        token_source="provider",
        pricing_source="unknown",
    )
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        summary = t.summary(conn)
    assert summary["ok_calls"] == 2
    assert summary["unpriced_calls"] == 1
    assert summary["total_usd"] == 0.02  # only the priced row contributes
    assert summary["total_tokens"] == 1150  # tokens still recorded for both


def test_operation_filter(cfg):
    _record(cfg, operation="observer", usd=0.01, tokens=100)
    _record(cfg, operation="reflector", usd=0.05, tokens=500)
    t = UsageTracker(cfg.usage_db_path)
    with t.connect() as conn:
        usd, _ = t.window_totals(conn, operation="reflector")
    assert usd == 0.05
