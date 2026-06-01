"""The conversation brain for `om talk`.

Each turn: kick off a background recall over memory, wait for it (bounded), then
ask the configured LLM (`llm.compress`) for a short, spoken-style reply grounded
in what recall found. The brain reuses the existing provider-routed, budget-gated
`compress` call rather than adding a new LLM path; `compress` is single-shot, so
rolling history and recalled snippets ride in the user content while the stable
instructions + profile ride in the (cacheable) system prompt.

It degrades gracefully: budget caps and provider errors are caught and turned
into a short spoken apology so a long conversation never crashes mid-turn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field

from .recall import RecallEngine, RecallResult, RecallSnippet, RecallStatus

_LOGGER = logging.getLogger(__name__)

_DEFAULT_MAX_HISTORY_TURNS = 6
_DEFAULT_RECALL_LIMIT = 5
_DEFAULT_PROFILE_BUDGET_CHARS = 4000
_REPLY_MAX_TOKENS = 700

_BASE_INSTRUCTIONS = (
    "You are Om, the user's personal memory companion. You are having a spoken, "
    "back-and-forth conversation, so keep replies short, natural, and free of "
    "markdown, bullet lists, or code blocks — they will be read aloud.\n\n"
    "Ground every answer in the RECALLED MEMORY provided with each turn. When the "
    "memory clearly answers the question, answer from it and you may briefly note "
    "where it came from. When the recalled memory has nothing relevant, say so "
    'plainly (for example, "I don\'t have anything in memory about that") instead '
    "of inventing details. Never fabricate facts about the user."
)


@dataclass
class ConversationTurn:
    """One user/assistant exchange plus the memory it was grounded in."""

    user: str
    assistant: str
    recalled: list[RecallSnippet] = field(default_factory=list)
    grounded: bool = False
    recall_status: str = RecallStatus.OK.value
    error: str | None = None


# compress(system_prompt, user_content, config, max_tokens=..., operation=...) -> str
CompressFn = Callable[..., str]


def _default_compress(system_prompt: str, user_content: str, config, **kwargs) -> str:
    from ..llm import compress

    return compress(system_prompt, user_content, config, **kwargs)


def _is_budget_error(exc: Exception) -> bool:
    """Detect a budget-cap refusal.

    Prefers a real ``isinstance`` against the usage subsystem's exception; falls
    back to a class-name match if that optional subsystem can't be imported.
    """
    try:
        from ..usage.budgets import BudgetExceededError

        if isinstance(exc, BudgetExceededError):
            return True
    except Exception:  # pragma: no cover - usage subsystem optional
        pass
    return type(exc).__name__ == "BudgetExceededError"


class Conversation:
    """Drives memory-grounded turns for the talk loop."""

    def __init__(
        self,
        config,
        recall_engine: RecallEngine,
        *,
        agent: str | None = None,
        max_history_turns: int = _DEFAULT_MAX_HISTORY_TURNS,
        recall_limit: int = _DEFAULT_RECALL_LIMIT,
        recall_timeout: float | None = None,
        compress: CompressFn | None = None,
    ) -> None:
        self._config = config
        self._recall = recall_engine
        self._agent = agent
        self._max_history_turns = max_history_turns
        self._recall_limit = recall_limit
        # Single source of truth for the per-turn recall budget: the config default
        # (OM_TALK_RECALL_TIMEOUT, fail-closed to 8.0) when not explicitly passed.
        self._recall_timeout = recall_timeout if recall_timeout is not None else config.talk_recall_timeout
        self._compress = compress or _default_compress
        self._history: list[ConversationTurn] = []
        self._profile_context = ""

    @property
    def history(self) -> list[ConversationTurn]:
        return list(self._history)

    def prepare(self) -> bool:
        """Warm the backend and load a compact profile pack. Returns backend readiness."""
        self._profile_context = self._load_profile_context()
        return self._recall.is_ready()

    def reply(self, utterance: str) -> ConversationTurn:
        """Produce one grounded reply. Never raises for expected LLM/budget errors."""
        utterance = (utterance or "").strip()
        recall_result = self._recall_for(utterance)

        system_prompt = self._system_prompt()
        user_content = self._build_user_content(utterance, recall_result)

        error: str | None = None
        try:
            text = self._compress(
                system_prompt,
                user_content,
                self._config,
                max_tokens=_REPLY_MAX_TOKENS,
                operation="talk",
            ).strip()
        except Exception as exc:  # graceful degradation — see module docstring
            if _is_budget_error(exc):
                error = "budget"
                text = "I've reached the spending limit set for memory, so I can't answer that one right now."
            else:
                error = "provider"
                _LOGGER.debug("talk compress failed: %s", exc)
                text = "Sorry — I couldn't reach my language model just now. Want to try again?"

        turn = ConversationTurn(
            user=utterance,
            assistant=text,
            recalled=list(recall_result.snippets),
            grounded=recall_result.grounded,
            recall_status=recall_result.status.value,
            error=error,
        )
        self._record(turn)
        return turn

    def close(self) -> None:
        self._recall.close()

    # -- internals --------------------------------------------------

    def _recall_for(self, utterance: str) -> RecallResult:
        if not utterance:
            ready = self._recall.is_ready()
            return RecallResult(
                query=utterance,
                backend_ready=ready,
                status=RecallStatus.EMPTY if ready else RecallStatus.UNAVAILABLE,
            )
        # A prior recall that overran its budget keeps occupying the single worker
        # (ThreadPoolExecutor can't cancel a running task). Don't queue behind it —
        # that would falsely report TIMEOUT on a turn that never even ran. Classify
        # as UNAVAILABLE (recall couldn't run this turn), distinct from a real timeout.
        if self._recall.has_pending_recall():
            _LOGGER.debug("prior recall still running; skipping recall for this turn")
            return RecallResult(query=utterance, backend_ready=True, status=RecallStatus.UNAVAILABLE)

        try:
            future = self._recall.recall_async(utterance, self._recall_limit)
        except Exception as exc:  # e.g. executor already shut down — degrade, don't crash reply()
            _LOGGER.debug("could not submit background recall: %s", exc)
            return RecallResult(query=utterance, backend_ready=True, status=RecallStatus.UNAVAILABLE)

        try:
            return future.result(timeout=self._recall_timeout)
        except FutureTimeoutError:
            future.cancel()
            # The recall thread was running but did not finish in our budget. This is
            # NOT "no memory found" — surface it as a timeout so the LLM and the
            # transcript can tell the difference.
            return RecallResult(query=utterance, backend_ready=True, status=RecallStatus.TIMEOUT)
        except Exception as exc:
            # recall() never raises today, so result() can only surface a timeout or
            # a cancellation. If it ever resolves to an exception, that's an abnormal
            # finish, not a timeout — report UNAVAILABLE rather than a false TIMEOUT.
            _LOGGER.debug("background recall finished abnormally: %s", exc)
            future.cancel()
            return RecallResult(query=utterance, backend_ready=True, status=RecallStatus.UNAVAILABLE)

    def _system_prompt(self) -> str:
        if self._profile_context:
            return f"{_BASE_INSTRUCTIONS}\n\n## Who you are talking to\n{self._profile_context}"
        return _BASE_INSTRUCTIONS

    def _build_user_content(self, utterance: str, recall_result: RecallResult) -> str:
        parts: list[str] = []

        if recall_result.snippets:
            parts.append("RECALLED MEMORY (most relevant first):")
            for i, snippet in enumerate(recall_result.snippets, start=1):
                label = snippet.heading or snippet.doc_id or f"memory {i}"
                parts.append(f"[{i}] {label} (source: {snippet.source})\n{snippet.content}")
        # No snippets — say *why*, so the model does not claim the memory is empty
        # when in fact recall never completed or the backend was down.
        elif recall_result.status is RecallStatus.TIMEOUT:
            parts.append(
                "RECALLED MEMORY: (memory search did not finish in time for this turn; "
                "do not assume memory is empty — say you couldn't check just now)"
            )
        elif recall_result.status is RecallStatus.UNAVAILABLE:
            # Intentional asymmetry with TIMEOUT above: a transient timeout asks Om to
            # say it couldn't check (the user can retry / raise the budget); a hard-down
            # or unindexed backend just answers conversationally, since the user can't
            # fix it mid-conversation. Documented in docs/talk-to-memories.md.
            parts.append("RECALLED MEMORY: (memory search is unavailable; answer conversationally)")
        else:  # RecallStatus.EMPTY
            parts.append("RECALLED MEMORY: (no relevant memory found for this turn)")

        history = self._recent_history()
        if history:
            parts.append("\nRECENT CONVERSATION:")
            for turn in history:
                parts.append(f"User: {turn.user}\nOm: {turn.assistant}")

        parts.append(f"\nUSER JUST SAID:\n{utterance}")
        return "\n\n".join(parts)

    def _recent_history(self) -> list[ConversationTurn]:
        if self._max_history_turns <= 0:
            return []
        return self._history[-self._max_history_turns :]

    def _record(self, turn: ConversationTurn) -> None:
        self._history.append(turn)
        # Keep an extra turn of slack so _recent_history always has full context.
        cap = max(self._max_history_turns * 2, 2)
        if len(self._history) > cap:
            self._history = self._history[-cap:]

    def _load_profile_context(self) -> str:
        try:
            from ..startup_memory import build_startup_payload

            payload = build_startup_payload(
                self._config,
                budget_chars=_DEFAULT_PROFILE_BUDGET_CHARS,
                agent=self._agent,
            )
            return (payload.text or "").strip()
        except Exception as exc:  # pragma: no cover - profile pack is best-effort
            _LOGGER.debug("could not build profile context for talk: %s", exc)
            return ""
