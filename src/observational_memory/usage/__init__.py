"""Host-local LLM usage tracking, cost estimation, and budget enforcement.

This subsystem records every LLM call routed through :func:`observational_memory.llm.compress`
to a local SQLite database, estimates USD cost from a shipped (overridable) pricing
snapshot, and enforces optional token/dollar budgets before a call is dispatched.

It is strictly host-local: ``usage.sqlite`` is never materialized or synced through
OM Cluster (see :data:`SYNC_EXCLUDED` and the guard test).
"""

from __future__ import annotations

from .budgets import (
    BudgetDecision,
    BudgetExceededError,
    check_budget,
    resolve_budgets,
)
from .models import CallRecord, CostEstimate, LLMUsage
from .pricing import PricingTable, load_pricing
from .reporting import format_status, format_tail, status_payload, tail_payload
from .tracker import (
    SESSION_ID,
    UsageTracker,
    record_call,
)

# Files this subsystem owns that must never be synced through OM Cluster.
SYNC_EXCLUDED = ("usage.sqlite", "usage.sqlite-wal", "usage.sqlite-shm")

__all__ = [
    "SESSION_ID",
    "SYNC_EXCLUDED",
    "BudgetDecision",
    "BudgetExceededError",
    "CallRecord",
    "CostEstimate",
    "LLMUsage",
    "PricingTable",
    "UsageTracker",
    "check_budget",
    "format_status",
    "format_tail",
    "load_pricing",
    "record_call",
    "resolve_budgets",
    "status_payload",
    "tail_payload",
]
