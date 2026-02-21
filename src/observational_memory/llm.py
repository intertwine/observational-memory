"""Thin LLM API abstraction over direct and enterprise providers."""

from __future__ import annotations

from .config import Config


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

    dispatcher = {
        "anthropic": _call_anthropic_direct,
        "openai": _call_openai_direct,
        "anthropic-vertex": _call_anthropic_vertex,
        "anthropic-bedrock": _call_anthropic_bedrock,
    }
    fn = dispatcher.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")

    try:
        return fn(system_prompt, user_content, model, max_tokens, config)
    except Exception as e:
        raise RuntimeError(f"LLM request failed for provider '{provider}' using model '{model}': {e}") from e


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

    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
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


def _extract_anthropic_text(message: object) -> str:
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("Anthropic response contained no content blocks.")
    first = content[0]
    text = getattr(first, "text", None)
    if not text:
        raise RuntimeError("Anthropic response did not include text content.")
    return text
