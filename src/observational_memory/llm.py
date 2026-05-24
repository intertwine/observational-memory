"""Thin LLM API abstraction over direct and enterprise providers."""

from __future__ import annotations

import logging
import random
import sys
import time
from typing import TYPE_CHECKING

from .config import Config, _env_flag

if TYPE_CHECKING:
    from .usage.models import LLMUsage

_LOGGER = logging.getLogger(__name__)

# Retry settings for transient errors (connection errors, rate limits).
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 2  # seconds; doubles each attempt (2, 4, 8, 16, 32)


def compress(
    system_prompt: str,
    user_content: str,
    config: Config | None = None,
    max_tokens: int = 4096,
    operation: str | None = None,
) -> str:
    """Send system_prompt + user_content to the configured LLM and return the response text."""
    if config is None:
        config = Config()

    # An explicit per-workflow provider (OM_LLM_OBSERVER_PROVIDER /
    # OM_LLM_REFLECTOR_PROVIDER) is authoritative for this operation: use it
    # directly, resolve its model without the global OM_LLM_MODEL (which usually
    # belongs to a different provider), and skip model-name inference.
    op_provider = config.operation_provider(operation)
    if op_provider:
        effective_provider = config.validate_provider_config(provider=op_provider)
        model = config.resolve_model(operation=operation, provider=effective_provider, ignore_global_model=True)
    else:
        provider = config.validate_provider_config()
        model = config.resolve_model(operation=operation, provider=provider)
        # When an operation-specific model override crosses provider boundaries
        # (e.g. reflector set to claude-sonnet-4-6 while default provider is openai),
        # infer the correct provider from the model name.
        effective_provider = _infer_provider(model, provider, auth_file=_config_auth_file(config))
        if effective_provider != provider:
            config.validate_provider_config(provider=effective_provider)

    dispatcher = {
        "anthropic": _call_anthropic_direct,
        "openai": _call_openai_direct,
        "anthropic-vertex": _call_anthropic_vertex,
        "anthropic-bedrock": _call_anthropic_bedrock,
        "openai-chatgpt": _call_openai_chatgpt,
        "xai-oauth": _call_xai_oauth,
        "xai": _call_xai_api_key,
    }
    fn = dispatcher.get(effective_provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {effective_provider}")

    _LOGGER.debug("LLM call: provider=%s model=%s operation=%s", effective_provider, model, operation or "default")

    # Pre-call budget gate (may raise BudgetExceededError on a hard cap). Token
    # and cost recording happens after the call returns / finally fails.
    _enforce_budget(config, operation, effective_provider, model, system_prompt, user_content, max_tokens)

    # ChatGPT Codex reasoning effort is per-operation; resolve it here (compress
    # knows the operation) and pass it only to that path so other provider
    # signatures (and their test fakes) are unaffected.
    reasoning_effort = config.resolve_reasoning_effort(operation) if effective_provider == "openai-chatgpt" else None

    last_error: Exception | None = None
    started = time.monotonic()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            if effective_provider == "openai-chatgpt":
                result = fn(system_prompt, user_content, model, max_tokens, config, reasoning_effort=reasoning_effort)
            else:
                result = fn(system_prompt, user_content, model, max_tokens, config)
            text, usage = _coerce_result(result)
            _record_usage(
                config,
                provider=effective_provider,
                model=model,
                operation=operation,
                usage=usage,
                response_text=text,
                system_prompt=system_prompt,
                user_content=user_content,
                started=started,
                retries=attempt,
                status="ok",
            )
            return text
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES and _is_retryable(e):
                delay = _RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                _LOGGER.warning(
                    "LLM request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                    e,
                )
                time.sleep(delay)
            else:
                break

    _record_usage(
        config,
        provider=effective_provider,
        model=model,
        operation=operation,
        usage=None,
        response_text="",
        system_prompt=system_prompt,
        user_content=user_content,
        started=started,
        retries=attempt,
        status="error",
    )
    raise RuntimeError(
        f"LLM request failed for provider '{effective_provider}' using model '{model}': {last_error}"
    ) from last_error


def _estimate_tokens(text: str) -> int:
    """Cheap prompt/completion token estimate (~4 chars/token) with no tokenizer dep."""
    return max(len(text or "") // 4, 0)


def _coerce_result(result: object) -> tuple[str, object | None]:
    """Normalize a provider helper's return into (text, usage|None).

    Provider helpers return ``(text, LLMUsage | None)``. Bare strings (e.g. from
    monkeypatched test fakes or third-party shims) are tolerated as text-only.
    """
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1]
    return result, None  # type: ignore[return-value]


def _enforce_budget(
    config: Config,
    operation: str | None,
    provider: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> None:
    """Check budgets before dispatch. Raises BudgetExceededError on a hard cap.

    Estimation is intentionally dependency-free: prompt tokens ≈ chars//4 plus
    the requested ``max_tokens`` for the completion. A one-shot ``OM_BUDGET_BYPASS=1``
    downgrades a hard block to a warning. All non-block failures are swallowed so
    budgeting can never break an LLM call.
    """
    if not config.usage_tracking:
        return
    try:
        from .usage import check_budget, record_call
        from .usage.budgets import BudgetExceededError
        from .usage.pricing import load_pricing
    except Exception:  # pragma: no cover - usage subsystem optional/defensive
        return

    prompt_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_content)
    est_tokens = prompt_tokens + max(max_tokens, 0)
    try:
        pricing = load_pricing(config.pricing_overrides_path)
        est = pricing.estimate(
            provider=provider, model=model, prompt_tokens=prompt_tokens, completion_tokens=max_tokens
        )
        decision = check_budget(config, operation=operation, est_usd=est.total_usd, est_tokens=est_tokens)
    except Exception:  # pragma: no cover - never let pricing/estimation break the call
        return

    for warning in decision.warnings:
        _LOGGER.warning("budget: %s", warning)
        print(f"om: budget warning — {warning}", file=sys.stderr)

    if not decision.blocked:
        return

    if _env_flag("OM_BUDGET_BYPASS"):
        _LOGGER.warning("budget: bypassing hard cap (OM_BUDGET_BYPASS=1) — %s", decision.block_reason)
        print(f"om: budget BYPASS (OM_BUDGET_BYPASS=1) — {decision.block_reason}", file=sys.stderr)
        return

    try:
        record_call(
            config,
            provider=provider,
            model=model,
            operation=operation,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            est_input_usd=est.input_usd,
            est_output_usd=est.output_usd,
            est_total_usd=est.total_usd,
            latency_ms=0,
            retries=0,
            status="blocked_by_budget",
            token_source="estimate",
            pricing_source=est.source,
        )
    except Exception:  # pragma: no cover - recording the block is best-effort
        pass
    raise BudgetExceededError(
        f"refusing to call {provider}/{model} — {decision.block_reason}. Override once with OM_BUDGET_BYPASS=1."
    )


def _record_usage(
    config: Config,
    *,
    provider: str,
    model: str,
    operation: str | None,
    usage: object | None,
    response_text: str,
    system_prompt: str,
    user_content: str,
    started: float,
    retries: int,
    status: str,
) -> None:
    """Persist a usage row for a completed (ok) or failed (error) call.

    Fully defensive: any failure here is logged at debug level and swallowed so
    recording can never break or mask the underlying LLM result.
    """
    if not config.usage_tracking:
        return
    try:
        from .usage import record_call
        from .usage.models import LLMUsage
        from .usage.pricing import load_pricing

        latency_ms = int((time.monotonic() - started) * 1000)

        if usage is None:
            if status == "ok":
                prompt_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_content)
                completion_tokens = _estimate_tokens(response_text)
                usage = LLMUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    token_source="estimate",
                )
            else:
                usage = LLMUsage(token_source="estimate")
        usage = usage.normalized()  # type: ignore[union-attr]

        pricing = load_pricing(config.pricing_overrides_path)
        est = pricing.estimate(
            provider=provider,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
        record_call(
            config,
            provider=provider,
            model=model,
            operation=operation,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            est_input_usd=est.input_usd,
            est_output_usd=est.output_usd,
            est_total_usd=est.total_usd,
            latency_ms=latency_ms,
            retries=retries,
            status=status,
            token_source=usage.token_source,
            pricing_source=est.source,
        )
    except Exception as exc:  # pragma: no cover - recording is best-effort
        _LOGGER.debug("usage recording failed: %s", exc)


def _config_auth_file(config: Config):
    """Best-effort auth-store path for the active config (None → default path)."""
    try:
        return config.auth_file
    except Exception:
        return None


def _infer_provider(model: str, default_provider: str, *, auth_file=None) -> str:
    """Infer the provider from a model name when it doesn't match the default.

    Subscription providers (`openai-chatgpt`, `xai-oauth`) are *sticky*: when the
    user explicitly selects one, model-name inference must never silently route
    away to a metered provider — that would charge per token and defeat the whole
    point of `om login`. A model name that doesn't fit the chosen subscription is
    sent to that subscription anyway and surfaces a clear provider-side error,
    instead of a surprise bill. See the v0.6.5 plan amendment.

    ``auth_file`` is the active config's auth-store path so subscription-token
    detection honors a non-default ``OM_AUTH_FILE`` / custom env file (otherwise
    a `grok-*` / Codex model could be misrouted to a metered provider).
    """
    import os as _os

    if default_provider in ("openai-chatgpt", "xai-oauth"):
        return default_provider

    normalized = model.lower()
    if normalized.startswith(("claude-", "claude3")):
        if default_provider not in ("anthropic", "anthropic-vertex", "anthropic-bedrock"):
            return "anthropic"
    elif normalized.startswith("grok-"):
        if default_provider == "xai":
            return default_provider
        from .config import _has_subscription_tokens  # local import to avoid cycles

        if _has_subscription_tokens("xai-oauth", auth_file):
            return "xai-oauth"
        if _os.environ.get("XAI_API_KEY"):
            return "xai"
        return default_provider
    elif normalized.startswith(("gpt-5-codex", "codex-")):
        from .config import _has_subscription_tokens

        if _has_subscription_tokens("openai-chatgpt", auth_file):
            return "openai-chatgpt"
    elif normalized.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        if default_provider != "openai":
            return "openai"
    return default_provider


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors worth retrying."""
    import httpx

    # Connection / timeout errors
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, httpx.ConnectError | httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return True

    # Provider SDK wrappers around connection errors
    try:
        import anthropic

        if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError)):
            return True
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (429, 500, 502, 503, 529):
            return True
    except ImportError:
        pass

    try:
        import openai

        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
            return True
        if isinstance(exc, openai.APIStatusError) and exc.status_code in (429, 500, 502, 503):
            return True
    except ImportError:
        pass

    # Catch-all: check the string for common transient patterns
    msg = str(exc).lower()
    if any(keyword in msg for keyword in ("connection", "timeout", "timed out", "rate limit", "429", "502", "503")):
        return True

    return False


def _call_anthropic_direct(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_anthropic_system_blocks(system_prompt),
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message), _anthropic_usage(message)


def _call_anthropic_vertex(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    import anthropic

    client = anthropic.AnthropicVertex(project_id=config.vertex_project_id, region=config.vertex_region)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_anthropic_system_blocks(system_prompt),
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message), _anthropic_usage(message)


def _call_anthropic_bedrock(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    import anthropic

    client = anthropic.AnthropicBedrock(aws_region=config.bedrock_region)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_anthropic_system_blocks(system_prompt),
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message), _anthropic_usage(message)


def _call_openai_direct(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    import openai

    client = openai.OpenAI(timeout=300.0)
    token_limit_arg = _openai_token_limit_arg(model, max_tokens)
    response = client.chat.completions.create(
        model=model,
        **token_limit_arg,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("OpenAI response contained empty content.")
    # OpenAI can return non-string content arrays in newer SDK response variants.
    text = content if isinstance(content, str) else str(content)
    return text, _openai_usage(response)


def _openai_token_limit_arg(model: str, max_tokens: int) -> dict[str, int]:
    normalized = model.lower()
    if normalized.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    default_headers: dict | None = None,
) -> tuple[str, "LLMUsage | None"]:
    """Shared OpenAI-compatible chat-completions call for chatgpt/xai-oauth/xai."""
    import openai

    client = openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=300.0,
        default_headers=default_headers or None,
    )
    token_limit_arg = _openai_token_limit_arg(model, max_tokens)
    response = client.chat.completions.create(
        model=model,
        **token_limit_arg,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError(f"{base_url} response contained empty content.")
    text = content if isinstance(content, str) else str(content)
    return text, _openai_usage(response)


def _call_openai_chatgpt(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
    reasoning_effort: str | None = None,
) -> tuple[str, "LLMUsage | None"]:
    from .auth import AuthError, resolve_runtime_credentials

    try:
        creds = resolve_runtime_credentials("openai-chatgpt", config=config)
    except AuthError:
        raise
    try:
        return _call_codex_responses(
            base_url=creds["base_url"],
            access_token=creds["access_token"],
            system_prompt=system_prompt,
            user_content=user_content,
            model=model,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        if _is_unauthorized(exc):
            creds = resolve_runtime_credentials("openai-chatgpt", force_refresh=True, config=config)
            return _call_codex_responses(
                base_url=creds["base_url"],
                access_token=creds["access_token"],
                system_prompt=system_prompt,
                user_content=user_content,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
        raise


def _call_codex_responses(
    *,
    base_url: str,
    access_token: str,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> tuple[str, "LLMUsage | None"]:
    """Call the ChatGPT Codex backend via the Responses API.

    The Codex backend (chatgpt.com/backend-api/codex) does NOT serve
    `/chat/completions` (returns 404) — it speaks the Responses API like the
    real Codex CLI. It also sits behind Cloudflare, which 403s any request that
    doesn't advertise a first-party `originator`. See the v0.6.5 plan amendment.

    ``reasoning_effort`` (low|medium|high|xhigh), when set, is forwarded as the
    Responses ``reasoning`` effort — lower effort cuts gpt-5.5 latency sharply.
    The Codex ``/models`` endpoint advertises this; it is omitted when None so
    the backend applies its own default.
    """
    import openai

    from .auth.openai_chatgpt import cloudflare_headers

    client = openai.OpenAI(
        api_key=access_token,
        base_url=base_url,
        timeout=300.0,
        default_headers=cloudflare_headers(access_token),
    )
    # The Codex backend imposes non-standard constraints on the Responses API
    # for ChatGPT-account auth (each surfaced as an HTTP 400 on 2026-05-23):
    #   * `input` must be a list of message items (not a bare string)
    #   * `store` must be False
    #   * `stream` must be True
    #   * `max_output_tokens` is rejected ("Unsupported parameter")
    # The system prompt rides in `instructions`; max_tokens is intentionally
    # not forwarded.
    del max_tokens  # not accepted by the Codex backend
    extra: dict = {}
    if reasoning_effort:
        extra["reasoning"] = {"effort": reasoning_effort}
    stream = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=[{"role": "user", "content": [{"type": "input_text", "text": user_content}]}],
        store=False,
        stream=True,
        **extra,
    )
    chunks: list[str] = []
    final_text: str | None = None
    usage: LLMUsage | None = None
    for event in stream:
        etype = getattr(event, "type", "")
        if etype == "response.output_text.delta":
            delta = getattr(event, "delta", "")
            if isinstance(delta, str):
                chunks.append(delta)
        elif etype in ("response.completed", "response.incomplete"):
            resp = getattr(event, "response", None)
            candidate = getattr(resp, "output_text", None)
            if isinstance(candidate, str) and candidate.strip():
                final_text = candidate
            # The Responses API attaches a usage object to the terminal event.
            usage = _responses_usage(getattr(resp, "usage", None)) or usage
    text = "".join(chunks) if chunks else final_text
    if text:
        return text, usage
    raise RuntimeError("ChatGPT Codex Responses API returned empty output.")


def _call_xai_oauth(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    from .auth import AuthError, resolve_runtime_credentials

    try:
        creds = resolve_runtime_credentials("xai-oauth", config=config)
    except AuthError:
        raise
    try:
        return _call_openai_compatible(
            base_url=creds["base_url"],
            api_key=creds["access_token"],
            system_prompt=system_prompt,
            user_content=user_content,
            model=model,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        if _is_unauthorized(exc):
            creds = resolve_runtime_credentials("xai-oauth", force_refresh=True, config=config)
            return _call_openai_compatible(
                base_url=creds["base_url"],
                api_key=creds["access_token"],
                system_prompt=system_prompt,
                user_content=user_content,
                model=model,
                max_tokens=max_tokens,
            )
        raise


def _call_xai_api_key(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> tuple[str, "LLMUsage | None"]:
    import os

    from .auth.oidc_discovery import validate_inference_base_url

    api_key = os.environ.get("XAI_API_KEY", "")
    base_url = validate_inference_base_url(
        os.environ.get("OM_XAI_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("XAI_BASE_URL", "").strip().rstrip("/"),
        fallback="https://api.x.ai/v1",
    )
    return _call_openai_compatible(
        base_url=base_url,
        api_key=api_key,
        system_prompt=system_prompt,
        user_content=user_content,
        model=model,
        max_tokens=max_tokens,
    )


def _is_unauthorized(exc: Exception) -> bool:
    try:
        import openai

        if isinstance(exc, openai.AuthenticationError):
            return True
        if isinstance(exc, openai.APIStatusError) and exc.status_code == 401:
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return "401" in msg or "unauthorized" in msg


def _anthropic_system_blocks(system_prompt: str) -> list[dict]:
    """System prompt as a cacheable block for Anthropic prompt caching.

    The observer/reflector system prompt is stable across calls, so marking it
    with ``cache_control: ephemeral`` lets repeat calls reuse it at a fraction of
    the input cost on the metered Anthropic providers. It is harmless when
    caching doesn't apply — the API treats it as an ordinary system block.
    """
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


def _extract_anthropic_text(message: object) -> str:
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("Anthropic response contained no content blocks.")
    first = content[0]
    text = getattr(first, "text", None)
    if not text:
        raise RuntimeError("Anthropic response did not include text content.")
    return text


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _anthropic_usage(message: object) -> "LLMUsage | None":
    """Map an Anthropic ``message.usage`` to LLMUsage (input/output tokens).

    With prompt caching active, ``input_tokens`` counts only the uncached
    remainder; the cached tokens are reported separately as
    ``cache_read_input_tokens`` and ``cache_creation_input_tokens``. We fold both
    into the prompt-token total so usage accounting stays accurate after caching
    is enabled (cost is still estimated at the flat input rate — the cache
    read/write discounts are not separately modeled).
    """
    from .usage.models import LLMUsage

    usage = getattr(message, "usage", None)
    pt = _safe_int(getattr(usage, "input_tokens", None))
    cache_read = _safe_int(getattr(usage, "cache_read_input_tokens", None))
    cache_create = _safe_int(getattr(usage, "cache_creation_input_tokens", None))
    ct = _safe_int(getattr(usage, "output_tokens", None))
    if pt is None and ct is None and cache_read is None and cache_create is None:
        return None
    prompt_tokens = (pt or 0) + (cache_read or 0) + (cache_create or 0)
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=ct,
        total_tokens=prompt_tokens + (ct or 0),
        token_source="provider",
    )


def _openai_usage(response: object) -> "LLMUsage | None":
    """Map an OpenAI-compatible ``response.usage`` to LLMUsage."""
    from .usage.models import LLMUsage

    usage = getattr(response, "usage", None)
    pt = _safe_int(getattr(usage, "prompt_tokens", None))
    ct = _safe_int(getattr(usage, "completion_tokens", None))
    tt = _safe_int(getattr(usage, "total_tokens", None))
    if pt is None and ct is None and tt is None:
        return None
    return LLMUsage(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt if tt is not None else (pt or 0) + (ct or 0),
        token_source="provider",
    )


def _responses_usage(usage: object) -> "LLMUsage | None":
    """Map a Responses-API usage object (input/output/total tokens) to LLMUsage."""
    from .usage.models import LLMUsage

    pt = _safe_int(getattr(usage, "input_tokens", None))
    ct = _safe_int(getattr(usage, "output_tokens", None))
    tt = _safe_int(getattr(usage, "total_tokens", None))
    if pt is None and ct is None and tt is None:
        return None
    return LLMUsage(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt if tt is not None else (pt or 0) + (ct or 0),
        token_source="provider",
    )
