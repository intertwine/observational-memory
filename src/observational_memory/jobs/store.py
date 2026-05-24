"""Host-local store for async provider jobs (JSON files, never synced)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Statuses a local job record can carry. ``submitted`` and ``completed`` are
# "pending" (still need applying); the rest are terminal.
PENDING_STATUSES = frozenset({"submitted", "completed"})
TERMINAL_STATUSES = frozenset({"applied", "drifted", "failed", "expired", "cancelled"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    """One async job. Stores only metadata and hashes — never prompt/response text."""

    job_id: str
    provider: str
    operation: str
    model: str
    endpoint: str
    custom_id: str
    batch_id: str | None = None
    input_file_id: str | None = None
    output_file_id: str | None = None
    status: str = "submitted"
    # Drift guards: hashes of the inputs as they were at submit time.
    reflections_sha256: str | None = None
    observations_sha256: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    applied_at: str | None = None
    result_artifact: str | None = None
    host: str = ""
    repo: str = ""
    error: str | None = None

    @property
    def pending(self) -> bool:
        return self.status in PENDING_STATUSES


class ProviderJobStore:
    """CRUD over ``<jobs_dir>/<job_id>.json`` records."""

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord) -> JobRecord:
        record.updated_at = _now_iso()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._path(record.job_id).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
        return record

    def load(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return JobRecord(**{k: v for k, v in data.items() if k in JobRecord.__dataclass_fields__})

    def delete(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)

    def list(self) -> list[JobRecord]:
        if not self.jobs_dir.is_dir():
            return []
        records: list[JobRecord] = []
        for path in self.jobs_dir.glob("*.json"):
            rec = self.load(path.stem)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def pending(self) -> list[JobRecord]:
        return [r for r in self.list() if r.pending]
