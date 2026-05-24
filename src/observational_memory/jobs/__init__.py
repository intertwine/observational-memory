"""Async provider jobs (host-local).

A small, host-local store and backend for offline LLM execution — currently the
direct API-key OpenAI Batch API for ``om reflect --async``. Job records live
under the OM data dir (``.provider-jobs/openai-batch/``) and are never synced
through OM Cluster. Batch is only ever used with the API-key ``openai`` provider,
never the ``openai-chatgpt`` subscription provider.
"""

from __future__ import annotations

from .openai_batch import (
    BatchProviderError,
    apply_completed_jobs,
    cancel_job,
    submit_reflect_batch,
)
from .store import JobRecord, ProviderJobStore

__all__ = [
    "BatchProviderError",
    "JobRecord",
    "ProviderJobStore",
    "apply_completed_jobs",
    "cancel_job",
    "submit_reflect_batch",
]
