"""Thin LLM API abstraction over Anthropic and OpenAI."""

from __future__ import annotations

from .config import Config


def compress(system_prompt: str, user_content: str, config: Config | None = None) -> str:
    """Send system_prompt + user_content to the configured LLM and return the response text."""
    if config is None:
        config = Config()

    provider = config.detect_provider()

    if provider == "anthropic":
        return _call_anthropic(system_prompt, user_content, config.anthropic_model)
    elif provider == "openai":
        return _call_openai(system_prompt, user_content, config.openai_model)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _call_anthropic(system_prompt: str, user_content: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text


def _call_openai(system_prompt: str, user_content: str, model: str) -> str:
    import openai

    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content
