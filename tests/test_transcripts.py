"""Tests for transcript parsers."""

from pathlib import Path

from observational_memory.transcripts.claude import parse_transcript as parse_claude
from observational_memory.transcripts.codex import parse_transcript as parse_codex

FIXTURES = Path(__file__).parent / "fixtures"


class TestClaudeParser:
    def test_parse_full_transcript(self):
        messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        assert len(messages) > 0
        assert all(m.source == "claude" for m in messages)

    def test_messages_have_roles(self):
        messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles

    def test_messages_have_timestamps(self):
        messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        for msg in messages:
            assert msg.timestamp, f"Message missing timestamp: {msg.content[:50]}"

    def test_tool_calls_summarized(self):
        messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        contents = [m.content for m in messages]
        # Tool uses should be summarized, not raw JSON
        assert any("[Bash:" in c or "[Write:" in c for c in contents)

    def test_incremental_parsing(self):
        all_messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        assert len(all_messages) > 2

        # Parse after the first message's UUID
        first_uuid = "msg-001"
        partial = parse_claude(FIXTURES / "claude-transcript.jsonl", after_uuid=first_uuid)
        assert len(partial) < len(all_messages)

    def test_user_content_preserved(self):
        messages = parse_claude(FIXTURES / "claude-transcript.jsonl")
        user_messages = [m for m in messages if m.role == "user"]
        contents = " ".join(m.content for m in user_messages)
        assert "FastAPI" in contents or "PostgreSQL" in contents


class TestCodexParser:
    def test_parse_full_transcript(self):
        messages = parse_codex(FIXTURES / "codex-transcript.jsonl")
        assert len(messages) > 0
        assert all(m.source == "codex" for m in messages)

    def test_messages_have_roles(self):
        messages = parse_codex(FIXTURES / "codex-transcript.jsonl")
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles

    def test_incremental_parsing(self):
        all_messages = parse_codex(FIXTURES / "codex-transcript.jsonl")
        partial = parse_codex(FIXTURES / "codex-transcript.jsonl", after_index=3)
        assert len(partial) < len(all_messages)

    def test_user_content_preserved(self):
        messages = parse_codex(FIXTURES / "codex-transcript.jsonl")
        user_messages = [m for m in messages if m.role == "user"]
        contents = " ".join(m.content for m in user_messages)
        assert "CSV" in contents or "S3" in contents
