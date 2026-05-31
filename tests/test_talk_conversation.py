"""Tests for the talk Conversation brain."""

from observational_memory.config import Config
from observational_memory.search import Document, DocumentSource, SearchResult
from observational_memory.talk.conversation import Conversation
from observational_memory.talk.recall import RecallEngine


class _FakeBackend:
    def __init__(self, ready=True, results=None):
        self._ready = ready
        self._results = results or []

    def is_ready(self):
        return self._ready

    def search(self, query, limit=10):
        return self._results[:limit]

    def index(self, documents):
        pass


class _BudgetExceededError(Exception):
    """Name-matches the real usage error so _is_budget_error detects it."""

    pass


_BudgetExceededError.__name__ = "BudgetExceededError"


def _result(doc_id, content):
    return SearchResult(
        document=Document(doc_id=doc_id, source=DocumentSource.REFLECTIONS, heading=f"## {doc_id}", content=content),
        score=1.0,
        rank=1,
    )


def _conversation(results=None, ready=True, compress=None, **kwargs):
    engine = RecallEngine(Config(), _FakeBackend(ready=ready, results=results))
    return Conversation(Config(), engine, compress=compress, **kwargs)


def test_reply_grounds_user_content_in_recalled_memory():
    captured = {}

    def fake_compress(system, user, config, **kw):
        captured["system"] = system
        captured["user"] = user
        captured["operation"] = kw.get("operation")
        return "Here is what I remember."

    convo = _conversation(
        results=[_result("ref:projects", "You are building the voice feature.")], compress=fake_compress
    )
    try:
        turn = convo.reply("what am I working on?")
        assert turn.assistant == "Here is what I remember."
        assert turn.grounded is True
        assert "RECALLED MEMORY" in captured["user"]
        assert "voice feature" in captured["user"]
        assert "what am I working on?" in captured["user"]
        assert captured["operation"] == "talk"
        assert "You are Om" in captured["system"]
    finally:
        convo.close()


def test_reply_notes_when_no_memory_found():
    captured = {}

    def fake_compress(system, user, config, **kw):
        captured["user"] = user
        return "ok"

    convo = _conversation(results=[], ready=True, compress=fake_compress)
    try:
        convo.reply("anything?")
        assert "no relevant memory found" in captured["user"]
    finally:
        convo.close()


def test_reply_notes_when_backend_unavailable():
    captured = {}

    def fake_compress(system, user, config, **kw):
        captured["user"] = user
        return "ok"

    convo = _conversation(results=[], ready=False, compress=fake_compress)
    try:
        turn = convo.reply("hi")
        assert turn.grounded is False
        assert "memory search is unavailable" in captured["user"]
    finally:
        convo.close()


def test_budget_error_degrades_gracefully():
    def boom(system, user, config, **kw):
        raise _BudgetExceededError("hard cap")

    convo = _conversation(compress=boom)
    try:
        turn = convo.reply("hello")
        assert turn.error == "budget"
        assert "spending limit" in turn.assistant
    finally:
        convo.close()


def test_provider_error_degrades_gracefully():
    def boom(system, user, config, **kw):
        raise RuntimeError("provider down")

    convo = _conversation(compress=boom)
    try:
        turn = convo.reply("hello")
        assert turn.error == "provider"
        assert "couldn't reach" in turn.assistant.lower()
    finally:
        convo.close()


def test_history_is_bounded():
    convo = _conversation(compress=lambda s, u, c, **k: "ok", max_history_turns=2)
    try:
        for i in range(10):
            convo.reply(f"turn {i}")
        # cap is max(max_history_turns*2, 2) == 4
        assert len(convo.history) <= 4
    finally:
        convo.close()


def test_recent_history_appears_in_prompt():
    seen = []

    def fake_compress(system, user, config, **kw):
        seen.append(user)
        return f"reply-{len(seen)}"

    convo = _conversation(compress=fake_compress)
    try:
        convo.reply("first thing")
        convo.reply("second thing")
        assert "RECENT CONVERSATION" in seen[1]
        assert "first thing" in seen[1]
    finally:
        convo.close()
