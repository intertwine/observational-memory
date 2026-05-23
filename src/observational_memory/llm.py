"""Thin LLM API abstraction over direct and enterprise providers."""

from __future__ import annotations

import logging
import random
import time

from .config import Config

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
        effective_provider = _infer_provider(model, provider)
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

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(system_prompt, user_content, model, max_tokens, config)
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

    raise RuntimeError(
        f"LLM request failed for provider '{effective_provider}' using model '{model}': {last_error}"
    ) from last_error


def _infer_provider(model: str, default_provider: str) -> str:
    """Infer the provider from a model name when it doesn't match the default.

    Subscription providers (`openai-chatgpt`, `xai-oauth`) are *sticky*: when the
    user explicitly selects one, model-name inference must never silently route
    away to a metered provider — that would charge per token and defeat the whole
    point of `om login`. A model name that doesn't fit the chosen subscription is
    sent to that subscription anyway and surfaces a clear provider-side error,
    instead of a surprise bill. See the v0.6.5 plan amendment.
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

        if _has_subscription_tokens("xai-oauth"):
            return "xai-oauth"
        if _os.environ.get("XAI_API_KEY"):
            return "xai"
        return default_provider
    elif normalized.startswith(("gpt-5-codex", "codex-")):
        from .config import _has_subscription_tokens

        if _has_subscription_tokens("openai-chatgpt"):
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
) -> str:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message)


def _call_anthropic_vertex(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> str:
    import anthropic

    client = anthropic.AnthropicVertex(project_id=config.vertex_project_id, region=config.vertex_region)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message)


def _call_anthropic_bedrock(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> str:
    import anthropic

    client = anthropic.AnthropicBedrock(aws_region=config.bedrock_region)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_anthropic_text(message)


def _call_openai_direct(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> str:
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
    if isinstance(content, str):
        return content
    if content is None:
        raise RuntimeError("OpenAI response contained empty content.")
    # OpenAI can return non-string content arrays in newer SDK response variants.
    return str(content)


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
) -> str:
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
    if isinstance(content, str):
        return content
    if content is None:
        raise RuntimeError(f"{base_url} response contained empty content.")
    return str(content)


def _call_openai_chatgpt(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> str:
    from .auth import AuthError, resolve_runtime_credentials

    try:
        creds = resolve_runtime_credentials("openai-chatgpt")
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
        )
    except Exception as exc:
        if _is_unauthorized(exc):
            creds = resolve_runtime_credentials("openai-chatgpt", force_refresh=True)
            return _call_codex_responses(
                base_url=creds["base_url"],
                access_token=creds["access_token"],
                system_prompt=system_prompt,
                user_content=user_content,
                model=model,
                max_tokens=max_tokens,
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
) -> str:
    """Call the ChatGPT Codex backend via the Responses API.

    The Codex backend (chatgpt.com/backend-api/codex) does NOT serve
    `/chat/completions` (returns 404) — it speaks the Responses API like the
    real Codex CLI. It also sits behind Cloudflare, which 403s any request that
    doesn't advertise a first-party `originator`. See the v0.6.5 plan amendment.
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
    stream = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=[{"role": "user", "content": [{"type": "input_text", "text": user_content}]}],
        store=False,
        stream=True,
    )
    chunks: list[str] = []
    final_text: str | None = None
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
    if chunks:
        return "".join(chunks)
    if final_text:
        return final_text
    raise RuntimeError("ChatGPT Codex Responses API returned empty output.")


def _call_xai_oauth(
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    config: Config,
) -> str:
    from .auth import AuthError, resolve_runtime_credentials

    try:
        creds = resolve_runtime_credentials("xai-oauth")
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
            creds = resolve_runtime_credentials("xai-oauth", force_refresh=True)
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
) -> str:
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


def _extract_anthropic_text(message: object) -> str:
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("Anthropic response contained no content blocks.")
    first = content[0]
    text = getattr(first, "text", None)
    if not text:
        raise RuntimeError("Anthropic response did not include text content.")
    return text
