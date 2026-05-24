"""Model pricing: a shipped snapshot plus an optional per-host override file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .models import CostEstimate

# Providers whose calls are paid for by a flat subscription, so marginal
# per-token cost is $0.00 (tokens are still recorded for observability).
SUBSCRIPTION_PROVIDERS = frozenset({"openai-chatgpt", "xai-oauth"})


@dataclass
class PricingTable:
    """Resolved pricing: builtin snapshot merged with an override file."""

    # model key -> {"input": usd_per_mtok, "output": usd_per_mtok}
    rates: dict[str, dict[str, float]] = field(default_factory=dict)
    # model key -> "builtin" | "override"
    sources: dict[str, str] = field(default_factory=dict)
    snapshot_date: str = "unknown"
    override_path: Path | None = None

    def _lookup(self, model: str) -> tuple[str | None, dict[str, float] | None, str | None]:
        """Resolve a (possibly date-suffixed) model name to a pricing entry.

        Tries an exact match, then the name with a trailing ``-YYYYMMDD`` date
        stripped, then the longest known key that the model name starts with.
        """
        if model in self.rates:
            return model, self.rates[model], self.sources.get(model)

        stripped = _strip_date_suffix(model)
        if stripped in self.rates:
            return stripped, self.rates[stripped], self.sources.get(stripped)

        best: str | None = None
        for key in self.rates:
            if model.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        if best is not None:
            return best, self.rates[best], self.sources.get(best)
        return None, None, None

    def estimate(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> CostEstimate:
        """Estimate USD cost for a call.

        Subscription providers short-circuit to $0.00. Otherwise we price the
        token counts against the resolved rate; an unknown model yields a
        ``source="unknown"`` estimate with ``None`` USD values.
        """
        if provider in SUBSCRIPTION_PROVIDERS:
            return CostEstimate(input_usd=0.0, output_usd=0.0, total_usd=0.0, source="subscription")

        _, rate, source = self._lookup(model)
        if rate is None:
            return CostEstimate(source="unknown")

        in_tok = prompt_tokens or 0
        out_tok = completion_tokens or 0
        input_usd = round(in_tok / 1_000_000 * float(rate.get("input", 0.0)), 6)
        output_usd = round(out_tok / 1_000_000 * float(rate.get("output", 0.0)), 6)
        return CostEstimate(
            input_usd=input_usd,
            output_usd=output_usd,
            total_usd=round(input_usd + output_usd, 6),
            source=source or "builtin",
        )


def _strip_date_suffix(model: str) -> str:
    """Strip a trailing ``-YYYYMMDD`` (e.g. claude-sonnet-4-5-20250929)."""
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        return parts[0]
    return model


def _parse_models_block(data: dict) -> dict[str, dict[str, float]]:
    block = data.get("models")
    if not isinstance(block, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for model, entry in block.items():
        if not isinstance(entry, dict):
            continue
        try:
            out[str(model)] = {
                "input": float(entry.get("input", 0.0)),
                "output": float(entry.get("output", 0.0)),
            }
        except (TypeError, ValueError):
            continue
    return out


def _load_builtin() -> tuple[dict[str, dict[str, float]], str]:
    raw = resources.files("observational_memory.usage").joinpath("pricing.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    return _parse_models_block(data), str(data.get("snapshot_date", "unknown"))


def load_pricing(override_path: Path | None = None) -> PricingTable:
    """Load the builtin snapshot and merge an optional override file on top."""
    rates, snapshot_date = _load_builtin()
    sources = {k: "builtin" for k in rates}

    if override_path is not None and override_path.is_file():
        try:
            data = tomllib.loads(override_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        for model, entry in _parse_models_block(data).items():
            rates[model] = entry
            sources[model] = "override"

    return PricingTable(
        rates=rates,
        sources=sources,
        snapshot_date=snapshot_date,
        override_path=override_path if (override_path and override_path.is_file()) else None,
    )


def write_override(path: Path, model: str, input_usd: float, output_usd: float) -> None:
    """Upsert one model into the override TOML, preserving existing entries.

    Written by hand (no tomli_w dependency); the override file is a flat
    ``[models]`` table of inline tables, which is trivial to round-trip.
    """
    existing: dict[str, dict[str, float]] = {}
    if path.is_file():
        try:
            existing = _parse_models_block(tomllib.loads(path.read_text(encoding="utf-8")))
        except (tomllib.TOMLDecodeError, OSError):
            existing = {}
    existing[model] = {"input": float(input_usd), "output": float(output_usd)}

    lines = [
        "# Observational Memory — per-host pricing overrides (USD per 1,000,000 tokens).",
        "# Entries here win over the shipped snapshot. Managed by `om usage pricing set`.",
        "",
        "[models]",
    ]
    for name in sorted(existing):
        entry = existing[name]
        lines.append(f'"{name}" = {{ input = {entry["input"]:g}, output = {entry["output"]:g} }}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
