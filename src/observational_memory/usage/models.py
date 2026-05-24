"""Plain dataclasses shared across the usage subsystem."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMUsage:
    """Token counts captured from (or estimated for) a single LLM call.

    ``token_source`` is ``"provider"`` when the numbers came from the SDK
    response and ``"estimate"`` when we fell back to the ``chars // 4`` heuristic
    (e.g. the ChatGPT Codex stream did not surface a usage object).
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    token_source: str = "provider"

    def normalized(self) -> "LLMUsage":
        """Fill ``total_tokens`` from the parts when it is missing."""
        total = self.total_tokens
        if total is None and (self.prompt_tokens is not None or self.completion_tokens is not None):
            total = (self.prompt_tokens or 0) + (self.completion_tokens or 0)
        return LLMUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=total,
            token_source=self.token_source,
        )


@dataclass
class CostEstimate:
    """Estimated USD cost for a call.

    ``source`` is one of ``builtin`` | ``override`` | ``subscription`` | ``unknown``.
    Subscription-backed providers (``openai-chatgpt``, ``xai-oauth``) always cost
    ``0.0`` with source ``subscription``. ``unknown`` means we recorded the tokens
    but had no price for the model, so the USD columns stay ``None``.
    """

    input_usd: float | None = None
    output_usd: float | None = None
    total_usd: float | None = None
    source: str = "unknown"


@dataclass
class CallRecord:
    """One row of the ``calls`` table (newest-first reporting shape)."""

    id: int
    ts_utc: str
    provider: str
    model: str
    operation: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    est_input_usd: float | None
    est_output_usd: float | None
    est_total_usd: float | None
    latency_ms: int | None
    retries: int
    status: str
    token_source: str
    pricing_source: str
    session_id: str
    host: str
    repo: str
