"""SQLite-backed record of every LLM call (host-local, never synced)."""

from __future__ import annotations

import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .models import CallRecord

if TYPE_CHECKING:
    from ..config import Config

# A session is one `om` process. Best-effort id = pid + process-start epoch
# (captured at first import), matching the issue's "PID + start time" contract.
_PROCESS_START = int(time.time())
SESSION_ID = f"{os.getpid()}-{_PROCESS_START}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    operation TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    est_input_usd REAL,
    est_output_usd REAL,
    est_total_usd REAL,
    latency_ms INTEGER,
    retries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    token_source TEXT NOT NULL DEFAULT 'provider',
    pricing_source TEXT NOT NULL DEFAULT 'unknown',
    session_id TEXT NOT NULL,
    host TEXT NOT NULL,
    repo TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts_utc);
CREATE INDEX IF NOT EXISTS idx_calls_session ON calls(session_id);
CREATE INDEX IF NOT EXISTS idx_calls_op ON calls(operation);
"""


def host_name() -> str:
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def repo_name() -> str:
    """Best-effort current-repo tag: the cwd basename."""
    try:
        name = Path.cwd().name
    except OSError:
        return "unknown"
    return name or "unknown"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def day_start_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()


def month_start_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()


class UsageTracker:
    """Thin wrapper over the usage SQLite database.

    Opened with WAL + a busy timeout so concurrent hook/scheduler processes can
    append rows without clobbering one another.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert(self, conn: sqlite3.Connection, fields: dict) -> int:
        cols = (
            "ts_utc, provider, model, operation, prompt_tokens, completion_tokens, total_tokens, "
            "est_input_usd, est_output_usd, est_total_usd, latency_ms, retries, status, "
            "token_source, pricing_source, session_id, host, repo"
        )
        placeholders = ", ".join(["?"] * 18)
        values = [
            fields["ts_utc"],
            fields["provider"],
            fields["model"],
            fields["operation"],
            fields.get("prompt_tokens"),
            fields.get("completion_tokens"),
            fields.get("total_tokens"),
            fields.get("est_input_usd"),
            fields.get("est_output_usd"),
            fields.get("est_total_usd"),
            fields.get("latency_ms"),
            fields.get("retries", 0),
            fields["status"],
            fields.get("token_source", "provider"),
            fields.get("pricing_source", "unknown"),
            fields["session_id"],
            fields["host"],
            fields["repo"],
        ]
        cur = conn.execute(f"INSERT INTO calls ({cols}) VALUES ({placeholders})", values)
        return int(cur.lastrowid or 0)

    def window_totals(
        self,
        conn: sqlite3.Connection,
        *,
        since_utc: str | None = None,
        session_id: str | None = None,
        operation: str | None = None,
    ) -> tuple[float, int]:
        """Sum est_total_usd and total_tokens over a window.

        Only ``ok`` rows count toward spend (blocked/error calls cost nothing).
        ``operation`` filters to a single operation when given.
        """
        clauses = ["status = 'ok'"]
        params: list[object] = []
        if since_utc is not None:
            clauses.append("ts_utc >= ?")
            params.append(since_utc)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if operation is not None:
            clauses.append("operation = ?")
            params.append(operation)
        where = " AND ".join(clauses)
        row = conn.execute(
            f"SELECT COALESCE(SUM(est_total_usd), 0.0) AS usd, "
            f"COALESCE(SUM(total_tokens), 0) AS tokens FROM calls WHERE {where}",
            params,
        ).fetchone()
        return float(row["usd"] or 0.0), int(row["tokens"] or 0)

    def tail(self, conn: sqlite3.Connection, limit: int = 20) -> list[CallRecord]:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def summary(
        self,
        conn: sqlite3.Connection,
        *,
        since_utc: str | None = None,
    ) -> dict:
        """Aggregate totals overall and grouped by operation."""
        clauses = []
        params: list[object] = []
        if since_utc is not None:
            clauses.append("ts_utc >= ?")
            params.append(since_utc)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        totals = conn.execute(
            f"SELECT COUNT(*) AS calls, "
            f"COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0) AS ok_calls, "
            f"COALESCE(SUM(CASE WHEN status='blocked_by_budget' THEN 1 ELSE 0 END), 0) AS blocked_calls, "
            f"COALESCE(SUM(total_tokens), 0) AS tokens, "
            f"COALESCE(SUM(est_total_usd), 0.0) AS usd FROM calls{where}",
            params,
        ).fetchone()

        per_op = conn.execute(
            f"SELECT operation, COUNT(*) AS calls, "
            f"COALESCE(SUM(total_tokens), 0) AS tokens, "
            f"COALESCE(SUM(est_total_usd), 0.0) AS usd FROM calls{where} "
            f"GROUP BY operation ORDER BY usd DESC",
            params,
        ).fetchall()

        return {
            "calls": int(totals["calls"]),
            "ok_calls": int(totals["ok_calls"]),
            "blocked_calls": int(totals["blocked_calls"]),
            "total_tokens": int(totals["tokens"]),
            "total_usd": round(float(totals["usd"] or 0.0), 6),
            "by_operation": [
                {
                    "operation": r["operation"],
                    "calls": int(r["calls"]),
                    "total_tokens": int(r["tokens"]),
                    "total_usd": round(float(r["usd"] or 0.0), 6),
                }
                for r in per_op
            ],
        }


def _row_to_record(row: sqlite3.Row) -> CallRecord:
    return CallRecord(
        id=int(row["id"]),
        ts_utc=row["ts_utc"],
        provider=row["provider"],
        model=row["model"],
        operation=row["operation"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        est_input_usd=row["est_input_usd"],
        est_output_usd=row["est_output_usd"],
        est_total_usd=row["est_total_usd"],
        latency_ms=row["latency_ms"],
        retries=int(row["retries"]),
        status=row["status"],
        token_source=row["token_source"],
        pricing_source=row["pricing_source"],
        session_id=row["session_id"],
        host=row["host"],
        repo=row["repo"],
    )


def record_call(
    config: "Config",
    *,
    provider: str,
    model: str,
    operation: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    est_input_usd: float | None,
    est_output_usd: float | None,
    est_total_usd: float | None,
    latency_ms: int | None,
    retries: int,
    status: str,
    token_source: str,
    pricing_source: str,
) -> int:
    """Persist one call row. Returns the row id (0 if tracking disabled)."""
    if not config.usage_tracking:
        return 0
    tracker = UsageTracker(config.usage_db_path)
    fields = {
        "ts_utc": now_utc_iso(),
        "provider": provider,
        "model": model,
        "operation": operation or "other",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "est_input_usd": est_input_usd,
        "est_output_usd": est_output_usd,
        "est_total_usd": est_total_usd,
        "latency_ms": latency_ms,
        "retries": retries,
        "status": status,
        "token_source": token_source,
        "pricing_source": pricing_source,
        "session_id": SESSION_ID,
        "host": host_name(),
        "repo": repo_name(),
    }
    with tracker.connect() as conn:
        return tracker.insert(conn, fields)
