"""Tests for transcript parsers."""

import logging
import time
from pathlib import Path

from observational_memory.transcripts.claude import (
    find_all_transcripts,
)
from observational_memory.transcripts.claude import (
    parse_transcript as parse_claude,
)
from observational_memory.transcripts.codex import line_offset_to_message_count
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


class TestFindAllTranscripts:
    def test_finds_all_jsonl_files(self, tmp_path):
        # Create mock project dirs with transcripts
        proj1 = tmp_path / "project-a"
        proj1.mkdir()
        (proj1 / "session-1.jsonl").write_text('{"type":"user"}\n')
        time.sleep(0.01)
        (proj1 / "session-2.jsonl").write_text('{"type":"user"}\n')

        proj2 = tmp_path / "project-b"
        proj2.mkdir()
        time.sleep(0.01)
        (proj2 / "session-3.jsonl").write_text('{"type":"user"}\n')

        results = find_all_transcripts(tmp_path)
        assert len(results) == 3

    def test_sorted_oldest_first(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()

        old = proj / "old.jsonl"
        old.write_text('{"type":"user"}\n')
        time.sleep(0.05)

        new = proj / "new.jsonl"
        new.write_text('{"type":"user"}\n')

        results = find_all_transcripts(tmp_path)
        assert results[0].name == "old.jsonl"
        assert results[1].name == "new.jsonl"

    def test_returns_empty_for_missing_dir(self, tmp_path):
        results = find_all_transcripts(tmp_path / "nonexistent")
        assert results == []

    def test_ignores_non_jsonl_files(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / "session.jsonl").write_text('{"type":"user"}\n')
        (proj / "notes.txt").write_text("not a transcript")
        (proj / "config.json").write_text("{}")

        results = find_all_transcripts(tmp_path)
        assert len(results) == 1


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

    def test_parse_modern_response_item_payload_messages(self, tmp_path):
        transcript = tmp_path / "codex-modern.jsonl"
        transcript.write_text(
            (
                '{"timestamp":"2026-03-11T04:08:58.126Z","type":"response_item",'
                '"payload":{"type":"message","role":"developer","content":'
                '[{"type":"input_text","text":"ignore me"}]}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:08:58.126Z","type":"response_item",'
                '"payload":{"type":"message","role":"user","content":'
                '[{"type":"input_text","text":"hello from user"}]}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:09:00.000Z","type":"response_item",'
                '"payload":{"type":"message","role":"assistant","content":'
                '[{"type":"output_text","text":"hello from assistant"}],'
                '"phase":"commentary"}}\n'
            )
        )

        messages = parse_codex(transcript)

        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == "hello from user"
        assert messages[1].content == "hello from assistant"
        assert messages[0].timestamp == "2026-03-11T04:08:58.126Z"

    def test_jsonl_transcripts_do_not_log_whole_file_json_warning(self, tmp_path, caplog):
        transcript = tmp_path / "codex-modern.jsonl"
        transcript.write_text(
            (
                '{"timestamp":"2026-03-11T04:08:58.126Z","type":"response_item",'
                '"payload":{"type":"message","role":"user","content":'
                '[{"type":"input_text","text":"hello"}]}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:09:00.000Z","type":"response_item",'
                '"payload":{"type":"message","role":"assistant","content":'
                '[{"type":"output_text","text":"world"}]}}\n'
            )
        )

        with caplog.at_level(logging.WARNING):
            messages = parse_codex(transcript)

        assert len(messages) == 2
        assert "Failed to parse Codex transcript" not in caplog.text

    def test_jsonl_file_with_single_full_json_payload_still_unwraps_items(self, tmp_path, caplog):
        transcript = tmp_path / "codex-single-doc.jsonl"
        transcript.write_text(
            '{"items":['
            '{"role":"user","content":"hello from user","timestamp":"2026-03-11T04:08:58.126Z"},'
            '{"role":"assistant","content":"hello from assistant","timestamp":"2026-03-11T04:09:00.000Z"}'
            "]}\n"
        )

        with caplog.at_level(logging.WARNING):
            messages = parse_codex(transcript)

        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == "hello from user"
        assert messages[1].content == "hello from assistant"
        assert "Failed to parse Codex transcript" not in caplog.text

    def test_line_offset_counts_modern_payload_messages(self, tmp_path):
        transcript = tmp_path / "codex-modern.jsonl"
        transcript.write_text(
            (
                '{"timestamp":"2026-03-11T04:08:58.126Z","type":"response_item",'
                '"payload":{"type":"message","role":"developer","content":'
                '[{"type":"input_text","text":"ignore me"}]}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:08:58.126Z","type":"response_item",'
                '"payload":{"type":"message","role":"user","content":'
                '[{"type":"input_text","text":"first"}]}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:09:00.000Z","type":"response_item",'
                '"payload":{"type":"function_call","name":"exec"}}\n'
            )
            + (
                '{"timestamp":"2026-03-11T04:09:01.000Z","type":"response_item",'
                '"payload":{"type":"message","role":"assistant","content":'
                '[{"type":"output_text","text":"second"}]}}\n'
            )
        )

        assert line_offset_to_message_count(transcript, 4) == 2
