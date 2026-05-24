"""Token/dollar budget resolution and pre-call enforcement.

Budgets are declared via environment variables (written by ``om usage budget``):

    OM_BUDGET_[<OPERATION>_]<WINDOW>_<UNIT>

where ``OPERATION`` is optional (``OBSERVER`` / ``REFLECTOR``; omit for a global
cap), ``WINDOW`` is ``DAILY`` / ``MONTHLY`` / ``SESSION``, and ``UNIT`` is ``USD``
/ ``TOKENS``. Each budget's mode is ``OM_BUDGET_MODE`` (hard|soft, default hard)
unless overridden by ``<KEY>_MODE``.

Examples::

    OM_BUDGET_DAILY_USD=5.00
    OM_BUDGET_MONTHLY_TOKENS=5_000_000
    OM_BUDGET_REFLECTOR_DAILY_USD=1.00
    OM_BUDGET_REFLECTOR_DAILY_USD_MODE=soft
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .tracker import (
    SESSION_ID,
    UsageTracker,
    day_start_utc_iso,
    month_start_utc_iso,
)

if TYPE_CHECKING:
    from ..config import Config

# Operations that can carry budgets. ``recall`` is intentionally excluded: it
# makes no LLM call today (read-only expansion of startup handles).
_OPERATIONS = ("observer", "reflector")
_WINDOWS = {"day": "DAILY", "month": "MONTHLY", "session": "SESSION"}
_UNITS = ("usd", "tokens")


class BudgetExceededError(RuntimeError):
    """Raised when a hard budget would be exceeded by the next call."""


@dataclass
class Budget:
    key: str
    scope: str  # "global" or an operation name
    window: str  # "day" | "month" | "session"
    unit: str  # "usd" | "tokens"
    limit: float
    mode: str  # "hard" | "soft"


@dataclass
class BudgetDecision:
    action: str = "allow"  # "allow" | "warn" | "block"
    warnings: list[str] = field(default_factory=list)
    block_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "block"


def _parse_number(raw: str) -> float | None:
    """Parse a USD/token value, tolerating ``_`` and ``,`` digit separators."""
    cleaned = raw.strip().replace("_", "").replace(",", "").lstrip("$")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def resolve_budgets(config: "Config") -> list[Budget]:
    """Read all configured budgets from the environment."""
    default_mode = (os.environ.get("OM_BUDGET_MODE", config.budget_mode) or "hard").strip().lower()
    if default_mode not in ("hard", "soft"):
        default_mode = "hard"

    scopes: list[tuple[str, str | None]] = [("global", None)]
    scopes += [(op, op) for op in _OPERATIONS]

    budgets: list[Budget] = []
    for scope, op in scopes:
        op_token = f"{op.upper()}_" if op else ""
        for window, win_token in _WINDOWS.items():
            for unit in _UNITS:
                key = f"OM_BUDGET_{op_token}{win_token}_{unit.upper()}"
                raw = os.environ.get(key)
                if raw is None:
                    continue
                limit = _parse_number(raw)
                if limit is None or limit <= 0:
                    continue
                mode = (os.environ.get(f"{key}_MODE", default_mode) or default_mode).strip().lower()
                if mode not in ("hard", "soft"):
                    mode = default_mode
                budgets.append(Budget(key=key, scope=scope, window=window, unit=unit, limit=limit, mode=mode))
    return budgets


def _window_since(window: str) -> tuple[str | None, str | None]:
    """Return (since_utc, session_id) filter args for a budget window."""
    if window == "day":
        return day_start_utc_iso(), None
    if window == "month":
        return month_start_utc_iso(), None
    if window == "session":
        return None, SESSION_ID
    return None, None


def _fmt(unit: str, value: float) -> str:
    if unit == "usd":
        return f"${value:,.2f}"
    return f"{int(value):,} tok"


def check_budget(
    config: "Config",
    *,
    operation: str | None,
    est_usd: float | None,
    est_tokens: int | None,
    soft_threshold: float | None = None,
) -> BudgetDecision:
    """Evaluate configured budgets against current spend plus this call's estimate.

    Returns a decision with the strongest action (block > warn > allow). Does not
    consult ``OM_BUDGET_BYPASS`` — the caller decides whether to honor a block.
    """
    decision = BudgetDecision()
    if not config.usage_tracking:
        return decision

    budgets = [b for b in resolve_budgets(config) if b.scope == "global" or b.scope == operation]
    if not budgets:
        return decision

    threshold = soft_threshold if soft_threshold is not None else config.budget_soft_threshold
    tracker = UsageTracker(config.usage_db_path)
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
            est = (est_usd or 0.0) if b.unit == "usd" else float(est_tokens or 0)
            projected = spent + est

            scope_label = "global" if b.scope == "global" else b.scope
            if projected > b.limit:
                msg = (
                    f"{b.window} {scope_label} budget — would reach {_fmt(b.unit, projected)} "
                    f"of {_fmt(b.unit, b.limit)} (spent {_fmt(b.unit, spent)}, "
                    f"estimate {_fmt(b.unit, est)})"
                )
                if b.mode == "hard":
                    decision.action = "block"
                    decision.block_reason = msg
                else:
                    decision.warnings.append(f"{msg} [soft]")
                    if decision.action != "block":
                        decision.action = "warn"
            elif b.limit > 0 and (spent / b.limit) >= threshold:
                pct = int(spent / b.limit * 100)
                decision.warnings.append(
                    f"{b.window} {scope_label} spend at {_fmt(b.unit, spent)} of {_fmt(b.unit, b.limit)} ({pct}%)"
                )
                if decision.action == "allow":
                    decision.action = "warn"

    return decision
