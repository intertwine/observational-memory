"""Tests for the pricing snapshot, override merge, and cost estimation."""

from __future__ import annotations

from observational_memory.usage.pricing import (
    SUBSCRIPTION_PROVIDERS,
    load_pricing,
    write_override,
)


def test_builtin_snapshot_loads_with_known_model():
    pricing = load_pricing(None)
    assert pricing.snapshot_date != "unknown"
    est = pricing.estimate(provider="openai", model="gpt-5.5", prompt_tokens=1_000_000, completion_tokens=0)
    assert est.source == "builtin"
    assert est.input_usd == 1.25  # gpt-5.5 input per 1M tokens


def test_date_suffixed_model_resolves_to_base_key():
    pricing = load_pricing(None)
    est = pricing.estimate(
        provider="anthropic",
        model="claude-sonnet-4-5-20250929",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert est.source == "builtin"
    assert est.input_usd == 3.00
    assert est.output_usd == 15.00


def test_unknown_model_records_no_cost():
    pricing = load_pricing(None)
    est = pricing.estimate(provider="openai", model="totally-made-up", prompt_tokens=500, completion_tokens=500)
    assert est.source == "unknown"
    assert est.total_usd is None


def test_subscription_providers_are_free():
    pricing = load_pricing(None)
    for provider in SUBSCRIPTION_PROVIDERS:
        est = pricing.estimate(provider=provider, model="gpt-5.5", prompt_tokens=10_000, completion_tokens=5_000)
        assert est.total_usd == 0.0
        assert est.source == "subscription"


def test_override_file_wins_over_snapshot(tmp_path):
    override = tmp_path / "pricing.toml"
    write_override(override, "gpt-5.5", 99.0, 199.0)
    pricing = load_pricing(override)
    est = pricing.estimate(provider="openai", model="gpt-5.5", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert est.source == "override"
    assert est.input_usd == 99.0
    assert est.output_usd == 199.0


def test_write_override_roundtrips_and_preserves_entries(tmp_path):
    override = tmp_path / "pricing.toml"
    write_override(override, "model-a", 1.0, 2.0)
    write_override(override, "model-b", 3.0, 4.0)
    pricing = load_pricing(override)
    assert pricing.rates["model-a"] == {"input": 1.0, "output": 2.0}
    assert pricing.rates["model-b"] == {"input": 3.0, "output": 4.0}
