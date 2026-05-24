"""Human-readable and JSON renderings of usage, budgets, and pricing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .budgets import _fmt, _window_since, resolve_budgets
from .pricing import load_pricing
from .tracker import UsageTracker

if TYPE_CHECKING:
    from ..config import Config


def _budget_lines(config: "Config") -> list[dict]:
    budgets = resolve_budgets(config)
    if not budgets:
        return []
    tracker = UsageTracker(config.usage_db_path)
    out: list[dict] = []
    with tracker.connect() as conn:
        for b in budgets:
            since, session = _window_since(b.window)
            spent_usd, spent_tokens = tracker.window_totals(
                conn,
                since_utc=since,
                session_id=session,
                operation=None if b.scope == "global" else b.scope,
            )
            spent = spent_usd if b.unit == "usd" else float(spent_tokens)
            remaining = max(b.limit - spent, 0.0)
            pct = int(spent / b.limit * 100) if b.limit else 0
            out.append(
                {
                    "scope": b.scope,
                    "window": b.window,
                    "unit": b.unit,
                    "mode": b.mode,
                    "limit": b.limit,
                    "spent": spent,
                    "remaining": remaining,
                    "pct": pct,
                }
            )
    return out


def status_payload(config: "Config", *, since_utc: str | None = None) -> dict:
    """Machine-readable usage status (for ``--json``)."""
    pricing = load_pricing(config.pricing_overrides_path)
    if not config.usage_tracking:
        return {
            "tracking": False,
            "db": str(config.usage_db_path),
            "pricing_snapshot": pricing.snapshot_date,
        }
    tracker = UsageTracker(config.usage_db_path)
    with tracker.connect() as conn:
        summary = tracker.summary(conn, since_utc=since_utc)
    return {
        "tracking": True,
        "db": str(config.usage_db_path),
        "since": since_utc,
        "pricing_snapshot": pricing.snapshot_date,
        "pricing_override": str(pricing.override_path) if pricing.override_path else None,
        "summary": summary,
        "budgets": _budget_lines(config),
    }


def tail_payload(config: "Config", *, limit: int = 20) -> list[dict]:
    if not config.usage_tracking:
        return []
    tracker = UsageTracker(config.usage_db_path)
    with tracker.connect() as conn:
        records = tracker.tail(conn, limit=limit)
    return [
        {
            "id": r.id,
            "ts_utc": r.ts_utc,
            "provider": r.provider,
            "model": r.model,
            "operation": r.operation,
            "total_tokens": r.total_tokens,
            "est_total_usd": r.est_total_usd,
            "latency_ms": r.latency_ms,
            "status": r.status,
            "token_source": r.token_source,
            "pricing_source": r.pricing_source,
            "repo": r.repo,
        }
        for r in records
    ]


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.4f}" if 0 < value < 0.01 else f"${value:,.2f}"


def format_status(config: "Config", *, since_utc: str | None = None) -> str:
    """Single-screen usage + budget summary."""
    data = status_payload(config, since_utc=since_utc)
    if not data["tracking"]:
        return (
            "Usage tracking is OFF (OM_USAGE_TRACKING=0).\n"
            f"  db (when enabled): {data['db']}\n"
            f"  pricing snapshot:  {data['pricing_snapshot']}"
        )

    s = data["summary"]
    lines: list[str] = []
    window = f"since {data['since']}" if data["since"] else "all time"
    lines.append(f"Usage ({window})")
    unpriced = s.get("unpriced_calls", 0)
    cost_str = _usd(s["total_usd"])
    if unpriced:
        # The total covers only priced calls; flag that some cost is unknown, not $0.
        cost_str += f" (+{unpriced} unpriced)"
    lines.append(
        f"  calls: {s['calls']}  (ok {s['ok_calls']}, blocked {s['blocked_calls']})   "
        f"tokens: {s['total_tokens']:,}   cost: {cost_str}"
    )
    if s["by_operation"]:
        lines.append("  by operation:")
        for op in s["by_operation"]:
            lines.append(
                f"    {op['operation']:<10} {op['calls']:>4} calls   "
                f"{op['total_tokens']:>10,} tok   {_usd(op['total_usd']):>10}"
            )

    budgets = data["budgets"]
    if budgets:
        lines.append("Budgets")
        for b in budgets:
            scope = "global" if b["scope"] == "global" else b["scope"]
            lines.append(
                f"  {scope:<10} {b['window']:<7} {b['mode']:<4}  "
                f"{_fmt(b['unit'], b['spent'])} / {_fmt(b['unit'], b['limit'])} "
                f"({b['pct']}%, {_fmt(b['unit'], b['remaining'])} left)"
            )
    else:
        lines.append("Budgets: none configured (om usage budget)")

    snap = data["pricing_snapshot"]
    override = data.get("pricing_override")
    lines.append(f"Pricing snapshot: {snap}" + (f"  (+override {override})" if override else ""))
    return "\n".join(lines)


def format_tail(config: "Config", *, limit: int = 20) -> str:
    rows = tail_payload(config, limit=limit)
    if not config.usage_tracking:
        return "Usage tracking is OFF (OM_USAGE_TRACKING=0)."
    if not rows:
        return "No LLM calls recorded yet."
    header = f"{'time (UTC)':<20} {'op':<9} {'model':<22} {'tokens':>8} {'cost':>9} {'lat':>6} {'status':<10}"
    lines = [header, "-" * len(header)]
    for r in rows:
        ts = (r["ts_utc"] or "")[:19].replace("T", " ")
        tok = f"{r['total_tokens']:,}" if r["total_tokens"] is not None else "—"
        cost = _usd(r["est_total_usd"])
        lat = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "—"
        lines.append(
            f"{ts:<20} {r['operation']:<9} {(r['model'] or '')[:22]:<22} {tok:>8} {cost:>9} {lat:>6} {r['status']:<10}"
        )
    return "\n".join(lines)
