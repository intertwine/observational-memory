"""Tests for the API-key OpenAI Batch backend (#55), all mocked."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from observational_memory.config import Config
from observational_memory.jobs import (
    BatchProviderError,
    ProviderJobStore,
    apply_completed_jobs,
    cancel_job,
    submit_reflect_batch,
)
from observational_memory.jobs.openai_batch import _sha256
from observational_memory.reflect import ChunkingRequired

_REFLECTIONS = (
    "# Reflections\n\n*Last updated: 2026-05-01 00:00 UTC*\n*Last reflected: 2026-05-01*\n\n## Core Identity\n- Test\n"
)
_OBSERVATIONS = "# Observations\n\n## 2026-05-20\n\n- 🔴 10:00 A brand new observation\n"


def _install_fake_openai(monkeypatch, state):
    class _FakeFiles:
        def create(self, file, purpose):
            state["uploaded"] = {"purpose": purpose, "file": file}
            return SimpleNamespace(id="file-input-1")

        def content(self, file_id):
            return SimpleNamespace(text=state.get("output_jsonl", ""))

        def delete(self, file_id):
            state.setdefault("deleted", []).append(file_id)

    class _FakeBatches:
        def create(self, input_file_id, endpoint, completion_window):
            state["created"] = {
                "input_file_id": input_file_id,
                "endpoint": endpoint,
                "completion_window": completion_window,
            }
            return SimpleNamespace(id="batch-1", status="validating")

        def retrieve(self, batch_id):
            return SimpleNamespace(
                id=batch_id,
                status=state.get("batch_status", "in_progress"),
                output_file_id=state.get("output_file_id"),
            )

        def cancel(self, batch_id):
            if state.get("cancel_raises"):
                raise RuntimeError("transient cancel failure")
            state["cancelled"] = batch_id
            return SimpleNamespace(id=batch_id, status="cancelling")

    class _FakeOpenAI:
        def __init__(self, **_kwargs):
            self.files = _FakeFiles()
            self.batches = _FakeBatches()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OM_USAGE_TRACKING", "0")
    monkeypatch.delenv("OM_CLUSTER_ENABLED", raising=False)
    config = Config(memory_dir=tmp_path / "mem", env_file=tmp_path / "cfg" / "env")
    config.ensure_memory_dir()
    config.reflections_path.write_text(_REFLECTIONS)
    config.observations_path.write_text(_OBSERVATIONS)
    return config


def _completed_output(custom_id: str, text: str) -> str:
    # Two lines in scrambled order; only the second matches custom_id.
    lines = [
        {"custom_id": "reflector:other:zzzz", "response": {"status_code": 200, "body": {"choices": []}}},
        {
            "custom_id": custom_id,
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [{"message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
            },
        },
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def test_submit_creates_job_with_batch_request(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)

    record = submit_reflect_batch(cfg)
    assert record is not None
    assert record.status == "submitted"
    assert record.batch_id == "batch-1"
    assert record.custom_id.startswith("reflector:")
    assert record.reflections_sha256 == _sha256(_REFLECTIONS)
    # Uploaded with purpose=batch; batch created with the 24h window on /v1/chat/completions.
    assert state["uploaded"]["purpose"] == "batch"
    assert state["created"]["completion_window"] == "24h"
    assert state["created"]["endpoint"] == "/v1/chat/completions"
    # The JSONL line carries a chat-completions request body with the system+user messages.
    _name, jsonl_bytes = state["uploaded"]["file"]
    line = json.loads(jsonl_bytes.decode("utf-8"))
    assert line["custom_id"] == record.custom_id
    assert line["url"] == "/v1/chat/completions"
    assert line["body"]["messages"][0]["role"] == "system"


def test_provider_guard_rejects_openai_chatgpt(cfg, monkeypatch):
    _install_fake_openai(monkeypatch, {})
    monkeypatch.setenv("OM_LLM_REFLECTOR_PROVIDER", "openai-chatgpt")
    # Config captures env at construction, so rebuild after setting the override.
    config = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)
    with pytest.raises(BatchProviderError) as exc:
        submit_reflect_batch(config)
    assert "openai-chatgpt" in str(exc.value)


def test_provider_guard_requires_api_key(cfg, monkeypatch):
    _install_fake_openai(monkeypatch, {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(BatchProviderError) as exc:
        submit_reflect_batch(cfg)
    assert "OPENAI_API_KEY" in str(exc.value)


def test_cross_provider_model_rejected_before_submit(cfg, monkeypatch):
    # Issue #77 repro: provider pinned to openai while an inherited reflector
    # model still points at another provider. Without the guard this submits
    # and fails at OpenAI's per-request validation hours later.
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    monkeypatch.setenv("OM_LLM_REFLECTOR_PROVIDER", "openai")
    monkeypatch.setenv("OM_LLM_REFLECTOR_MODEL", "grok-4.3")
    config = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)
    with pytest.raises(BatchProviderError) as exc:
        submit_reflect_batch(config)
    assert "grok-4.3" in str(exc.value)
    assert "OM_LLM_REFLECTOR_MODEL" in str(exc.value)
    # Failed fast: nothing was uploaded, no batch was created.
    assert "uploaded" not in state
    assert "created" not in state


def test_openai_model_passes_pre_submit_guard(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    monkeypatch.setenv("OM_LLM_REFLECTOR_PROVIDER", "openai")
    monkeypatch.setenv("OM_LLM_REFLECTOR_MODEL", "gpt-5.5")
    config = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)
    record = submit_reflect_batch(config)
    assert record is not None
    assert record.model == "gpt-5.5"
    assert "created" in state


def test_chunking_required_for_large_input(cfg, monkeypatch):
    _install_fake_openai(monkeypatch, {})
    # A huge observations file (well past the default per-call input ceiling)
    # forces the chunked path -> not single-pass.
    big = "# Observations\n\n## 2026-05-20\n\n" + ("- 🔴 10:00 " + "x" * 100 + "\n") * 2000
    cfg.observations_path.write_text(big)
    with pytest.raises(ChunkingRequired):
        submit_reflect_batch(cfg)


def test_poll_applies_completed_job_mapping_by_custom_id(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    assert record is not None

    new_reflections = "# Reflections\n\n## Core Identity\n- Updated by batch\n"
    state["batch_status"] = "completed"
    state["output_file_id"] = "file-output-1"
    state["output_jsonl"] = _completed_output(record.custom_id, new_reflections)

    results = apply_completed_jobs(cfg)
    assert results == [{"job_id": record.job_id, "status": "applied"}]
    # reflections.md was rewritten from the batch output (timestamps re-stamped).
    written = cfg.reflections_path.read_text()
    assert "Updated by batch" in written
    # Remote input + output files were cleaned up.
    assert set(state["deleted"]) == {"file-input-1", "file-output-1"}
    # Local record marked applied.
    applied = ProviderJobStore(cfg.openai_batch_jobs_dir).load(record.job_id)
    assert applied.status == "applied"


def test_poll_refuses_on_drift_and_writes_artifact(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    assert record is not None

    # Simulate another reflect having run between submit and apply.
    cfg.reflections_path.write_text(_REFLECTIONS + "\n## Drifted\n- changed since submit\n")

    state["batch_status"] = "completed"
    state["output_file_id"] = "file-output-1"
    state["output_jsonl"] = _completed_output(record.custom_id, "# Reflections\n\n## Core Identity\n- Stale batch\n")

    results = apply_completed_jobs(cfg)
    assert results[0]["status"] == "drifted"
    # reflections.md was NOT overwritten with the stale batch output.
    assert "Stale batch" not in cfg.reflections_path.read_text()
    assert "changed since submit" in cfg.reflections_path.read_text()
    # The output was preserved as a review artifact.
    artifact = cfg.openai_batch_jobs_dir / f"{record.job_id}.result.md"
    assert artifact.exists() and "Stale batch" in artifact.read_text()
    # Remote files are not cleaned up on drift (kept for review).
    assert "deleted" not in state


def test_submit_does_not_advance_state_until_apply(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    before = cfg.observations_path.read_text()
    submit_reflect_batch(cfg)
    # Submitting must not trim observations or write reflections.
    assert cfg.observations_path.read_text() == before
    assert cfg.reflections_path.read_text() == _REFLECTIONS


def test_cancel_marks_cancelled(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    cancelled = cancel_job(cfg, record.job_id)
    assert cancelled.status == "cancelled"
    assert state["cancelled"] == "batch-1"


def test_submit_returns_none_when_nothing_to_reflect(cfg, monkeypatch):
    _install_fake_openai(monkeypatch, {})
    # Last reflected is newer than the only observation -> nothing new, no auto-memory.
    cfg.reflections_path.write_text(
        "# Reflections\n\n*Last updated: 2026-05-22 00:00 UTC*\n*Last reflected: 2026-05-22*\n\n## Core Identity\n- x\n"
    )
    assert submit_reflect_batch(cfg) is None


def test_apply_marks_failed_on_failed_batch(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    state["batch_status"] = "failed"
    results = apply_completed_jobs(cfg)
    assert results[0]["status"] == "failed"
    assert ProviderJobStore(cfg.openai_batch_jobs_dir).load(record.job_id).status == "failed"


def test_apply_reports_request_level_error(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    err_line = {"custom_id": record.custom_id, "response": {"status_code": 429, "body": {}}, "error": None}
    state["batch_status"] = "completed"
    state["output_file_id"] = "file-output-1"
    state["output_jsonl"] = json.dumps(err_line) + "\n"
    results = apply_completed_jobs(cfg)
    assert results[0]["status"] == "failed"
    assert "429" in results[0]["detail"]
    assert cfg.reflections_path.read_text() == _REFLECTIONS  # untouched


def test_auto_memory_change_blocks_apply(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)  # records auto_memory_sha256 of "" (no auto-memory)

    # Make apply observe changed auto-memory (reflections/observations unchanged).
    from observational_memory import reflect as reflect_mod

    original = reflect_mod._gather_reflection_inputs

    def with_changed_auto_memory(config):
        inputs = original(config)
        if inputs is not None:
            inputs.auto_memory = "NEW cross-project facts"
        return inputs

    monkeypatch.setattr(reflect_mod, "_gather_reflection_inputs", with_changed_auto_memory)
    state["batch_status"] = "completed"
    state["output_file_id"] = "file-output-1"
    state["output_jsonl"] = _completed_output(record.custom_id, "# Reflections\n\n## Core Identity\n- stale\n")
    results = apply_completed_jobs(cfg)
    assert results[0]["status"] == "drifted"
    assert "stale" not in cfg.reflections_path.read_text()


def test_env_async_mode_falls_back_to_sync_on_provider_error(cfg, monkeypatch):
    from observational_memory import cli, jobs

    def boom(config):
        raise jobs.BatchProviderError("no usable openai provider")

    monkeypatch.setattr("observational_memory.jobs.submit_reflect_batch", boom)
    ran: dict = {}
    monkeypatch.setattr(
        "observational_memory.reflect.run_reflector",
        lambda config: ran.setdefault("ran", True) and "# Reflections",
    )
    # Env-driven async (explicit=False) must degrade to a sync run, not raise.
    cli._reflect_async(cfg, explicit=False)
    assert ran.get("ran") is True


def test_explicit_async_raises_on_provider_error(cfg, monkeypatch):
    import click

    from observational_memory import cli, jobs

    def boom(config):
        raise jobs.BatchProviderError("no usable openai provider")

    monkeypatch.setattr("observational_memory.jobs.submit_reflect_batch", boom)
    with pytest.raises(click.ClickException):
        cli._reflect_async(cfg, explicit=True)


def test_submit_blocked_by_hard_budget_before_upload(cfg, tmp_path, monkeypatch):
    from observational_memory.usage.budgets import BudgetExceededError
    from observational_memory.usage.tracker import UsageTracker

    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    monkeypatch.setenv("OM_USAGE_TRACKING", "1")
    monkeypatch.setenv("OM_USAGE_DB", str(tmp_path / "usage.sqlite"))
    monkeypatch.setenv("OM_LLM_MODEL", "gpt-4o-mini")  # priced model, regardless of any leaked global model
    # Token cap blocks regardless of pricing; USD cap also set for completeness.
    monkeypatch.setenv("OM_BUDGET_DAILY_TOKENS", "1")
    monkeypatch.setenv("OM_BUDGET_DAILY_USD", "0.0000001")
    monkeypatch.setenv("OM_BUDGET_MODE", "hard")
    monkeypatch.delenv("OM_BUDGET_BYPASS", raising=False)
    config = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)

    with pytest.raises(BudgetExceededError):
        submit_reflect_batch(config)
    # The cost-incurring calls must never have been reached.
    assert "uploaded" not in state
    assert "created" not in state
    # A blocked row was recorded (parity with the sync path).
    tracker = UsageTracker(config.usage_db_path)
    with tracker.connect() as conn:
        summary = tracker.summary(conn)
    assert summary["blocked_calls"] == 1


def test_async_model_honors_reflector_provider_override(cfg, monkeypatch):
    state: dict = {}
    _install_fake_openai(monkeypatch, state)
    # Global model belongs to a different provider; the openai reflector override
    # must ignore it (mirrors the sync ignore_global_model rule).
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai-chatgpt")
    monkeypatch.setenv("OM_LLM_MODEL", "claude-sonnet-4-5-20250929")
    monkeypatch.setenv("OM_LLM_REFLECTOR_PROVIDER", "openai")
    config = Config(memory_dir=cfg.memory_dir, env_file=cfg.env_file)

    record = submit_reflect_batch(config)
    assert record is not None
    assert record.model == config.openai_model  # openai default, not the global claude model
    assert record.model != "claude-sonnet-4-5-20250929"


def test_budget_block_is_clean_clickexception_both_modes(cfg, monkeypatch):
    import click

    from observational_memory import cli
    from observational_memory.usage.budgets import BudgetExceededError

    def blocked(config):
        raise BudgetExceededError("daily budget exceeded")

    monkeypatch.setattr("observational_memory.jobs.submit_reflect_batch", blocked)
    # A budget block is terminal in BOTH modes (a sync fallback would also block).
    for explicit in (True, False):
        with pytest.raises(click.ClickException):
            cli._reflect_async(cfg, explicit=explicit)


def test_sync_run_propagates_budget_error_not_reflection_failed(cfg, monkeypatch):
    from observational_memory import cli
    from observational_memory.usage.budgets import BudgetExceededError

    # BudgetExceededError subclasses RuntimeError; _run_reflector_sync must let it
    # through (terminal, dedicated wording) rather than rewrap it as the generic
    # "Reflection failed" used for retry-exhausted RuntimeErrors.
    def blocked(config):
        raise BudgetExceededError("daily budget exceeded")

    monkeypatch.setattr("observational_memory.reflect.run_reflector", blocked)
    with pytest.raises(BudgetExceededError):
        cli._run_reflector_sync(cfg)


def test_degrade_path_budget_block_keeps_dedicated_wording(cfg, monkeypatch):
    import click

    from observational_memory import cli, jobs
    from observational_memory.reflect import ChunkingRequired
    from observational_memory.usage.budgets import BudgetExceededError

    # Async degrades to a synchronous run (ChunkingRequired); the sync llm.compress
    # pre-call gate then hits the budget cap. The budget block must surface with its
    # own wording, not the generic "Reflection failed" rewrap from _run_reflector_sync.
    def needs_chunking(config):
        raise ChunkingRequired("input too large")

    def blocked(config):
        raise BudgetExceededError("daily budget exceeded")

    monkeypatch.setattr(jobs, "submit_reflect_batch", needs_chunking)
    monkeypatch.setattr("observational_memory.reflect.run_reflector", blocked)
    with pytest.raises(click.ClickException) as excinfo:
        cli._reflect_async(cfg, explicit=False)
    assert "daily budget exceeded" in str(excinfo.value)
    assert "Reflection failed" not in str(excinfo.value)


def _install_failing_openai(monkeypatch, error):
    """Install a fake openai whose batch submit (POST /batches) raises ``error``.

    The file upload succeeds; ``batches.create`` raises the given provider error,
    mirroring a real ``POST /batches`` rejection. The fake namespace re-exports the
    real ``APIStatusError`` so ``cli._reflect_async``'s ``except`` clause resolves.
    """
    import openai

    class _FakeFiles:
        def create(self, file, purpose):
            return SimpleNamespace(id="file-input-1")

    class _FakeBatches:
        def create(self, input_file_id, endpoint, completion_window):
            raise error

    class _FakeOpenAI:
        def __init__(self, **_kwargs):
            self.files = _FakeFiles()
            self.batches = _FakeBatches()

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=_FakeOpenAI, APIStatusError=openai.APIStatusError),
    )


def _openai_error(cls, message, code):
    """Build a provider error the way the real OpenAI SDK does.

    The SDK's ``_make_status_error_from_response`` sets ``exc.message`` to the
    verbose ``"Error code: <status> - {'error': {...}}"`` envelope (the full body
    dict embedded, NOT the clean inner message) and ``exc.body`` to the inner
    error mapping (``body.get("error", body)``). Constructing it this way keeps
    the fallback-message test honest about what production actually sees.
    """
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/batches")
    status = 429 if cls is openai.RateLimitError else 400
    payload = {"error": {"message": message, "code": code, "type": "invalid_request_error"}}
    response = httpx.Response(status, request=request, json=payload)
    err = openai.OpenAI(api_key="sk-test")._make_status_error_from_response(response)
    assert isinstance(err, cls)
    return err


def test_async_submit_billing_error_is_clean_clickexception(cfg, monkeypatch):
    import click
    import openai

    from observational_memory import cli

    error = _openai_error(openai.BadRequestError, "Billing hard limit has been reached", "billing_hard_limit_reached")
    _install_failing_openai(monkeypatch, error)

    with pytest.raises(click.ClickException) as excinfo:
        cli._reflect_async(cfg, explicit=True)
    # Actionable one-line message, no raw traceback / provider class leaking out.
    assert "billing hard limit reached" in str(excinfo.value)
    assert "plan/limits" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    # Regression: a failed submit leaves no dangling job and writes nothing new.
    assert ProviderJobStore(cfg.openai_batch_jobs_dir).list() == []
    assert cfg.reflections_path.read_text() == _REFLECTIONS


def test_async_submit_rate_limit_error_is_clean_clickexception(cfg, monkeypatch):
    import click
    import openai

    from observational_memory import cli

    error = _openai_error(openai.RateLimitError, "You exceeded your current quota", "insufficient_quota")
    _install_failing_openai(monkeypatch, error)

    with pytest.raises(click.ClickException) as excinfo:
        cli._reflect_async(cfg, explicit=True)
    assert "billing hard limit reached" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    # Regression: no dangling job record, reflections.md untouched.
    assert ProviderJobStore(cfg.openai_batch_jobs_dir).list() == []
    assert cfg.reflections_path.read_text() == _REFLECTIONS


def test_async_submit_other_provider_error_uses_provider_message(cfg, monkeypatch):
    import click
    import openai

    from observational_memory import cli

    error = _openai_error(openai.BadRequestError, "model is not supported for batch", "model_not_found")
    _install_failing_openai(monkeypatch, error)

    with pytest.raises(click.ClickException) as excinfo:
        cli._reflect_async(cfg, explicit=True)
    assert "OpenAI Batch submission failed: model is not supported for batch" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    # Regression: the clean inner message surfaces, not the verbose SDK envelope
    # ("Error code: 400 - {'error': {...}}") that exc.message actually carries.
    assert "Error code:" not in str(excinfo.value)
    assert "{'error'" not in str(excinfo.value)


def test_sync_fallback_surfaces_retry_exhausted_runtimeerror_cleanly(cfg, monkeypatch):
    import click

    from observational_memory import cli

    # Mirror the retry-exhausted RuntimeError raised by llm.compress after retries.
    def boom(config):
        raise RuntimeError("LLM request failed for provider 'openai' using model 'gpt-4o': 429 rate limited")

    monkeypatch.setattr("observational_memory.reflect.run_reflector", boom)
    with pytest.raises(click.ClickException) as excinfo:
        cli._run_reflector_sync(cfg)
    assert "Reflection failed:" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


def test_cancel_leaves_job_pending_on_failure(cfg, monkeypatch):
    state: dict = {"cancel_raises": True}
    _install_fake_openai(monkeypatch, state)
    record = submit_reflect_batch(cfg)
    result = cancel_job(cfg, record.job_id)
    assert result.status == "submitted"  # still pending, retryable
    assert result.error and "cancel request failed" in result.error
    assert ProviderJobStore(cfg.openai_batch_jobs_dir).load(record.job_id).pending
