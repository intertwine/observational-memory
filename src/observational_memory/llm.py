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
    """Infer the provider from a model name when it doesn't match the default."""
    normalized = model.lower()
    if normalized.startswith(("claude-", "claude3")):
        # Could be direct Anthropic, Vertex, or Bedrock — only override if
        # the default provider isn't already an Anthropic variant.
        if default_provider not in ("anthropic", "anthropic-vertex", "anthropic-bedrock"):
            return "anthropic"
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


def _extract_anthropic_text(message: object) -> str:
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("Anthropic response contained no content blocks.")
    first = content[0]
    text = getattr(first, "text", None)
    if not text:
        raise RuntimeError("Anthropic response did not include text content.")
    return text
