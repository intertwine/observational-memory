"""OpenAI Batch backend for offline reflection (API-key 'openai' provider only).

Submit a single-pass reflection as an OpenAI Batch job, then poll and apply the
result later. Never used for the ``openai-chatgpt`` subscription provider. Apply
runs the full synchronous reflect pipeline, but only if local state hasn't
drifted since submit; otherwise the output is saved as a review artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from ..config import Config
from .store import JobRecord, ProviderJobStore


class BatchProviderError(RuntimeError):
    """Raised when Batch is misconfigured (wrong provider, missing key, etc.)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _client():
    import openai

    return openai.OpenAI(timeout=300.0)


def _resolve_openai_provider(config: Config) -> str:
    """Require the API-key ``openai`` provider for the reflector. Raise otherwise.

    This is the guard that keeps Batch off the ``openai-chatgpt`` subscription
    path (which has no Batch API) and any other provider.
    """
    provider = config.operation_provider("reflector") or config.resolve_provider()
    if provider != "openai":
        extra = " (subscription auth has no Batch API)" if provider == "openai-chatgpt" else ""
        raise BatchProviderError(
            f"Async Batch requires the API-key 'openai' provider, but the reflector resolves to "
            f"'{provider}'{extra}. Set OM_LLM_REFLECTOR_PROVIDER=openai with OPENAI_API_KEY to use Batch."
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise BatchProviderError("Async Batch requires OPENAI_API_KEY for the 'openai' provider.")
    return provider


def submit_reflect_batch(config: Config) -> JobRecord | None:
    """Submit a single-pass reflection as a Batch job.

    Returns the saved :class:`JobRecord`, or ``None`` when there's nothing to
    reflect on. Raises :class:`BatchProviderError` for provider/auth problems and
    :class:`reflect.ChunkingRequired` when the input is too large for one request
    (the caller should fall back to a synchronous run).
    """
    from .. import reflect
    from ..llm import build_openai_chat_request
    from ..usage.tracker import host_name, repo_name

    _resolve_openai_provider(config)
    prepared = reflect.prepare_single_pass_reflection(config)  # may raise ChunkingRequired
    if prepared is None:
        return None
    system_prompt, user_content, max_output, inputs = prepared
    model = config.resolve_model(operation="reflector", provider="openai")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    custom_id = f"reflector:{ts}:{_sha256(user_content)[:8]}"
    body = build_openai_chat_request(model, system_prompt, user_content, max_output)
    line = {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body}
    jsonl = (json.dumps(line) + "\n").encode("utf-8")

    client = _client()
    input_file = client.files.create(file=("om-reflect-batch.jsonl", jsonl), purpose="batch")
    batch = client.batches.create(
        input_file_id=getattr(input_file, "id", None),
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )

    store = ProviderJobStore(config.openai_batch_jobs_dir)
    record = JobRecord(
        job_id=uuid4().hex[:16],
        provider="openai",
        operation="reflector",
        model=model,
        endpoint="/v1/chat/completions",
        custom_id=custom_id,
        batch_id=getattr(batch, "id", None),
        input_file_id=getattr(input_file, "id", None),
        status="submitted",
        reflections_sha256=_sha256(inputs.reflections),
        observations_sha256=_sha256(inputs.observations),
        auto_memory_sha256=_sha256(inputs.auto_memory),
        host=host_name(),
        repo=repo_name(),
    )
    return store.save(record)


def apply_completed_jobs(config: Config) -> list[dict]:
    """Poll every pending job; apply completed ones (drift-checked). Returns a summary."""
    store = ProviderJobStore(config.openai_batch_jobs_dir)
    pending = store.pending()
    if not pending:
        return []
    client = _client()
    results: list[dict] = []
    for record in pending:
        if not record.batch_id:
            results.append({"job_id": record.job_id, "status": "error", "detail": "no batch_id recorded"})
            continue
        try:
            batch = client.batches.retrieve(record.batch_id)
        except Exception as exc:  # network/transient — leave pending, report
            results.append({"job_id": record.job_id, "status": "error", "detail": f"retrieve failed: {exc}"})
            continue
        status = getattr(batch, "status", "") or ""
        if status == "completed":
            results.append(_apply_one(config, client, store, record, batch))
        elif status in ("failed", "expired", "cancelled"):
            record.status = status
            record.error = f"batch {status}"
            store.save(record)
            results.append({"job_id": record.job_id, "status": status})
        else:
            results.append({"job_id": record.job_id, "status": "pending", "detail": status})
    return results


def _apply_one(config: Config, client, store: ProviderJobStore, record: JobRecord, batch) -> dict:
    from .. import reflect

    output_file_id = getattr(batch, "output_file_id", None)
    record.output_file_id = output_file_id
    if not output_file_id:
        record.status = "failed"
        record.error = "completed batch had no output file"
        store.save(record)
        return {"job_id": record.job_id, "status": "failed", "detail": record.error}

    try:
        raw = _download_text(client, output_file_id)
    except Exception as exc:
        record.error = f"download failed: {exc}"
        store.save(record)
        return {"job_id": record.job_id, "status": "error", "detail": record.error}

    line = _find_response_line(raw, record.custom_id)
    if line is None:
        record.status = "failed"
        record.error = "no response matched the recorded custom_id"
        store.save(record)
        return {"job_id": record.job_id, "status": "failed", "detail": record.error}

    # Distinguish a request-level error / non-200 from a malformed body so the
    # reported reason is the real one.
    err = line.get("error")
    response = line.get("response") or {}
    status_code = response.get("status_code")
    if err or (status_code is not None and status_code != 200):
        record.status = "failed"
        record.error = f"batch request failed: {err or f'HTTP {status_code}'}"
        store.save(record)
        return {"job_id": record.job_id, "status": "failed", "detail": record.error}

    body = response.get("body")
    if not isinstance(body, dict):
        record.status = "failed"
        record.error = "batch response had no body"
        store.save(record)
        return {"job_id": record.job_id, "status": "failed", "detail": record.error}

    try:
        text = _extract_text(body)
    except ValueError as exc:
        record.status = "failed"
        record.error = str(exc)
        store.save(record)
        return {"job_id": record.job_id, "status": "failed", "detail": record.error}

    # Drift guard: refuse to apply if any reflector input — reflections.md, the
    # new-observation frontier, or auto-memory — changed since submit. Save the
    # output as a review artifact instead.
    current_reflections = config.reflections_path.read_text() if config.reflections_path.exists() else ""
    inputs = reflect._gather_reflection_inputs(config)
    current_observations = inputs.observations if inputs is not None else ""
    current_auto_memory = inputs.auto_memory if inputs is not None else ""
    drifted = (
        _sha256(current_reflections) != (record.reflections_sha256 or "")
        or _sha256(current_observations) != (record.observations_sha256 or "")
        or _sha256(current_auto_memory) != (record.auto_memory_sha256 or "")
    )
    if drifted:
        config.openai_batch_jobs_dir.mkdir(parents=True, exist_ok=True)
        artifact = config.openai_batch_jobs_dir / f"{record.job_id}.result.md"
        artifact.write_text(text, encoding="utf-8")
        record.status = "drifted"
        record.result_artifact = str(artifact)
        store.save(record)
        return {"job_id": record.job_id, "status": "drifted", "artifact": str(artifact)}

    # Apply with full parity to a synchronous reflect.
    raw_observations = config.observations_path.read_text() if config.observations_path.exists() else ""
    reflect.finalize_reflection(text, config, raw_observations)
    _record_batch_usage(config, record, body)
    _cleanup_remote(client, record)
    record.status = "applied"
    record.applied_at = _now_iso()
    store.save(record)
    return {"job_id": record.job_id, "status": "applied"}


def cancel_job(config: Config, job_id: str) -> JobRecord:
    store = ProviderJobStore(config.openai_batch_jobs_dir)
    record = store.load(job_id)
    if record is None:
        raise BatchProviderError(f"No job '{job_id}'.")
    if record.batch_id and record.pending:
        try:
            _client().batches.cancel(record.batch_id)
        except Exception as exc:
            record.error = f"cancel request failed: {exc}"
    record.status = "cancelled"
    return store.save(record)


def _download_text(client, file_id: str) -> str:
    content = client.files.content(file_id)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    read = getattr(content, "read", None)
    if callable(read):
        data = read()
        return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    return str(content)


def _find_response_line(raw_jsonl: str, custom_id: str) -> dict | None:
    """Return the output line matching ``custom_id`` (order is not guaranteed).

    Returns the whole line object so the caller can distinguish a request-level
    error / non-200 from a malformed body; None when no line matches.
    """
    for raw_line in raw_jsonl.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("custom_id") == custom_id:
            return obj
    return None


def _extract_text(body: dict) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected batch response body shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("batch response had empty content")
    return content


def _record_batch_usage(config: Config, record: JobRecord, body: dict) -> None:
    """Record the applied call in the usage DB at the Batch (50%) price."""
    try:
        from ..usage import record_call
        from ..usage.pricing import load_pricing

        usage = body.get("usage") or {}
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        tt = usage.get("total_tokens")
        pricing = load_pricing(config.pricing_overrides_path)
        est = pricing.estimate(provider="openai", model=record.model, prompt_tokens=pt, completion_tokens=ct)

        def _half(value: float | None) -> float | None:
            return round(value * 0.5, 6) if value is not None else None

        record_call(
            config,
            provider="openai",
            model=record.model,
            operation="reflector",
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt if tt is not None else (pt or 0) + (ct or 0),
            est_input_usd=_half(est.input_usd),
            est_output_usd=_half(est.output_usd),
            est_total_usd=_half(est.total_usd),
            latency_ms=None,
            retries=0,
            status="ok",
            token_source="provider",
            pricing_source=est.source,
        )
    except Exception:  # pragma: no cover - recording is best-effort
        pass


def _cleanup_remote(client, record: JobRecord) -> None:
    """Delete the uploaded input/output files from OpenAI after a successful apply."""
    for file_id in (record.input_file_id, record.output_file_id):
        if not file_id:
            continue
        try:
            client.files.delete(file_id)
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
