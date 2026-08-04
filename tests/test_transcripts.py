"""Tests for transcript parsers."""

import json
import logging
import time
from pathlib import Path

from observational_memory.transcripts import codex as codex_transcripts
from observational_memory.transcripts.claude import (
    count_messages as count_claude_messages,
)
from observational_memory.transcripts.claude import (
    find_all_transcripts,
    last_message_uuid,
)
from observational_memory.transcripts.claude import (
    parse_transcript as parse_claude,
)
from observational_memory.transcripts.codex import count_messages as count_codex_messages
from observational_memory.transcripts.codex import line_offset_to_message_count
from observational_memory.transcripts.codex import parse_transcript as parse_codex
from observational_memory.transcripts.hermes import parse_transcript as parse_hermes

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

    def test_count_and_last_uuid_match_parser_without_read_text(self, monkeypatch, tmp_path):
        transcript = tmp_path / "claude.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "system", "uuid": "system-1", "message": {"content": "ignore"}}),
                    json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hello"}}),
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "a1",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                        }
                    ),
                    json.dumps({"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": "skip"}}),
                    "{bad json",
                ]
            )
            + "\n"
        )

        original_read_text = Path.read_text

        def fail_read_text(path, *args, **kwargs):
            if path == transcript:
                raise AssertionError("Claude parser/count helpers should stream JSONL files")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read_text)

        assert len(parse_claude(transcript)) == 2
        assert count_claude_messages(transcript) == 2
        assert last_message_uuid(transcript) == "meta"

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

    def test_jsonl_parser_streams_from_message_cursor_without_read_text(self, monkeypatch, tmp_path):
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "turn_context", "payload": {"role": "system", "content": "ignore"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "message", "role": "user", "content": "one"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "message", "role": "assistant", "content": "two"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "message", "role": "user", "content": "three"},
                        }
                    ),
                ]
            )
            + "\n"
        )

        original_read_text = Path.read_text

        def fail_read_text(path, *args, **kwargs):
            if path == transcript:
                raise AssertionError("Codex JSONL parsing should stream instead of read_text")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read_text)

        messages = parse_codex(transcript, after_index=1)
        assert [message.content for message in messages] == ["two", "three"]

        def fail_message_construction(*args, **kwargs):
            raise AssertionError("Codex message counting should not retain parsed Message objects")

        monkeypatch.setattr(codex_transcripts, "Message", fail_message_construction)
        assert count_codex_messages(transcript) == 3

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

    def test_line_offset_counts_bare_items_wrapper_same_as_parser(self, tmp_path):
        transcript = tmp_path / "codex-bare-items.jsonl"
        transcript.write_text(
            (json.dumps({"type": "turn_context", "payload": {"role": "system", "content": "ignore"}}) + "\n")
            + (
                json.dumps(
                    {
                        "items": [
                            {
                                "role": "user",
                                "content": "wrapped user",
                                "timestamp": "2026-03-11T04:08:59.000Z",
                            },
                            {
                                "role": "assistant",
                                "content": "wrapped assistant",
                                "timestamp": "2026-03-11T04:09:00.000Z",
                            },
                        ]
                    }
                )
                + "\n"
            )
            + (
                json.dumps(
                    {
                        "timestamp": "2026-03-11T04:09:01.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "third"}],
                        },
                    }
                )
                + "\n"
            )
        )

        messages = parse_codex(transcript)
        assert len(messages) == 3

        # Line offset 2 covers the typed non-message line plus the bare items
        # wrapper (which unwraps to two messages), so migration counting must
        # agree with the parser's own message numbering for that wrapper shape.
        assert line_offset_to_message_count(transcript, 2) == 2

    def test_line_offset_skips_empty_content_records_like_parser(self, tmp_path):
        transcript = tmp_path / "codex-empty-content.jsonl"
        transcript.write_text(
            (
                json.dumps(
                    {
                        "role": "user",
                        "content": "first",
                        "timestamp": "2026-03-11T04:08:58.126Z",
                    }
                )
                + "\n"
            )
            + (
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "timestamp": "2026-03-11T04:08:59.000Z",
                    }
                )
                + "\n"
            )
            + (
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "second",
                        "timestamp": "2026-03-11T04:09:00.000Z",
                    }
                )
                + "\n"
            )
        )

        messages = parse_codex(transcript)
        assert [m.content for m in messages] == ["first", "second"]
        assert count_codex_messages(transcript) == 2

        # The empty-content assistant record on line 2 is skipped by the
        # parser, so a legacy cursor covering all three lines must migrate to
        # the parser's numbering (2), not the raw extractable-record count (3)
        # — otherwise the migrated cursor overshoots and drops real messages.
        assert line_offset_to_message_count(transcript, 3) == 2


class TestHermesParser:
    def test_parse_full_transcript(self):
        messages = parse_hermes(FIXTURES / "hermes-session.jsonl")

        assert len(messages) == 3
        assert all(m.source == "hermes" for m in messages)
        assert [m.role for m in messages] == ["user", "assistant", "assistant"]

    def test_tool_calls_are_summarized_and_machine_records_filtered(self):
        messages = parse_hermes(FIXTURES / "hermes-session.jsonl")

        contents = [m.content for m in messages]

        assert any("[terminal: gh run view 123 --log]" in content for content in contents)
        assert any("[web_search: observational-memory pypi]" in content for content in contents)
        assert all("raw tool output" not in content for content in contents)
        assert all("[read: README.md]" not in content for content in contents)

    def test_incremental_parsing_skips_already_counted_messages(self, tmp_path):
        transcript = tmp_path / "hermes-session.jsonl"
        transcript.write_text(
            (
                '{"role":"user","content":"one","timestamp":"2026-04-04T00:00:00Z"}\n'
                '{"role":"assistant","content":"two","timestamp":"2026-04-04T00:00:01Z"}\n'
                '{"role":"user","content":"three","timestamp":"2026-04-04T00:00:02Z"}\n'
            )
        )

        messages = parse_hermes(transcript, after_index=1)

        assert [m.content for m in messages] == ["two", "three"]


class TestCoworkParser:
    """Tests for Cowork audit.jsonl parsing via the Claude parser with source='cowork'."""

    def test_parse_cowork_transcript(self):
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        assert len(messages) > 0
        assert all(m.source == "cowork" for m in messages)

    def test_messages_have_roles(self):
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles

    def test_audit_timestamp_fallback(self):
        """Cowork uses _audit_timestamp instead of timestamp — parser should handle it."""
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        for msg in messages:
            assert msg.timestamp, f"Message missing timestamp: {msg.content[:50]}"
        # Check that the actual _audit_timestamp values came through
        assert any("2026-03-15" in m.timestamp for m in messages)

    def test_tool_calls_summarized(self):
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        contents = [m.content for m in messages]
        assert any("[Bash:" in c for c in contents)

    def test_incremental_parsing(self):
        all_messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        assert len(all_messages) > 2
        partial = parse_claude(FIXTURES / "cowork-audit.jsonl", after_uuid="cwk-001", source="cowork")
        assert len(partial) < len(all_messages)

    def test_system_and_rate_limit_entries_skipped(self):
        """System init and rate_limit_event entries should not produce messages."""
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl", source="cowork")
        for m in messages:
            assert m.role in ("user", "assistant")

    def test_source_parameter_defaults_to_claude(self):
        """Without explicit source, parse_transcript should label messages as 'claude'."""
        messages = parse_claude(FIXTURES / "cowork-audit.jsonl")
        assert all(m.source == "claude" for m in messages)


class TestCoworkDiscovery:
    """Tests for Cowork transcript discovery functions."""

    def _make_cowork_tree(self, tmp_path):
        """Create a mock Cowork sessions directory tree."""
        org = tmp_path / "org-uuid"
        user = org / "user-uuid"
        sessions = []
        for i, name in enumerate(["local_session-1", "local_session-2"]):
            session_dir = user / name
            session_dir.mkdir(parents=True)
            audit = session_dir / "audit.jsonl"
            audit.write_text(f'{{"type":"user","uuid":"m{i}","message":{{"role":"user","content":"hi"}}}}\n')
            sessions.append(audit)
            if i == 0:
                time.sleep(0.05)  # Ensure different mtimes
        return tmp_path, sessions

    def test_find_all_transcripts(self, tmp_path):
        from observational_memory.transcripts.cowork import find_all_transcripts as find_all

        base, sessions = self._make_cowork_tree(tmp_path)
        results = find_all(base)
        assert len(results) == 2
        # Oldest first
        assert results[0] == sessions[0]

    def test_find_recent_transcripts(self, tmp_path):
        from observational_memory.transcripts.cowork import find_recent_transcripts as find_recent

        base, sessions = self._make_cowork_tree(tmp_path)
        results = find_recent(base, max_age_hours=1)
        assert len(results) == 2
        # Newest first
        assert results[0] == sessions[1]

    def test_find_recent_respects_age_cutoff(self, tmp_path):
        import os

        from observational_memory.transcripts.cowork import find_recent_transcripts as find_recent

        base, sessions = self._make_cowork_tree(tmp_path)
        # Set the first session's mtime to 48 hours ago
        old_time = time.time() - (48 * 3600)
        os.utime(sessions[0], (old_time, old_time))
        results = find_recent(base, max_age_hours=24)
        assert len(results) == 1
        assert results[0] == sessions[1]

    def test_returns_empty_for_missing_dir(self, tmp_path):
        from observational_memory.transcripts.cowork import find_all_transcripts as find_all

        results = find_all(tmp_path / "nonexistent")
        assert results == []


class TestGrokParser:
    """Tests for the Grok Build TUI transcript parser (updates.jsonl with session/update events)."""

    def test_parse_grok_updates_jsonl(self, tmp_path):
        from observational_memory.transcripts.grok import parse_transcript as parse_grok

        transcript = tmp_path / "grok-sample.jsonl"
        # Minimal real-world-like events from inspection of actual Grok sessions on this machine
        transcript.write_text(
            '{"timestamp":1778885590,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"user_message_chunk","content":{"type":"text",'
            '"text":"Please orient yourself to this local machine."}}}}\n'
            + '{"timestamp":1778885591,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"agent_thought_chunk","content":{"type":"text",'
            '"text":"The user wants a machine orientation report."}}}}\n'
            + '{"timestamp":1778885592,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"tool_call","name":"list_dir"}}}\n'
            + '{"timestamp":1778885593,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"agent_message_chunk","content":{"type":"text",'
            '"text":"Here is the report on this workstation..."}}}}\n'
        )
        messages = parse_grok(transcript, source="grok")
        assert len(messages) >= 3
        assert all(m.source == "grok" for m in messages)
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles
        contents = " ".join(m.content for m in messages)
        assert "orient yourself" in contents.lower() or "machine" in contents.lower()
        assert any("tool" in m.content.lower() for m in messages if "tool" in m.content.lower())

    def test_grok_parser_handles_empty_or_invalid(self, tmp_path):
        from observational_memory.transcripts.grok import parse_transcript as parse_grok

        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        messages = parse_grok(transcript)
        assert messages == []

        bad = tmp_path / "bad.jsonl"
        bad.write_text("not json\n")
        messages = parse_grok(bad)
        assert messages == []

    def test_find_recent_grok_sessions(self, tmp_path):
        import time

        from observational_memory.transcripts.grok import find_recent_grok_sessions

        sessions_dir = tmp_path / "sessions"
        # Real Grok structure: <cwd-encoded>/<session-id>/updates.jsonl
        (sessions_dir / "cwd1" / "session1").mkdir(parents=True)
        f1 = sessions_dir / "cwd1" / "session1" / "updates.jsonl"
        f1.write_text("{}")

        time.sleep(0.05)
        (sessions_dir / "cwd1" / "session2").mkdir(parents=True)
        f2 = sessions_dir / "cwd1" / "session2" / "updates.jsonl"
        f2.write_text("{}")

        results = find_recent_grok_sessions(sessions_dir)
        assert len(results) == 2
        # Newest first (by mtime)
        assert results[0].parent.name == "session2"

    def test_grok_parser_supports_timestamp_cursor_and_tool_call_cleanup(self, tmp_path):
        from observational_memory.transcripts.grok import parse_transcript as parse_grok

        transcript = tmp_path / "grok-cursor.jsonl"
        transcript.write_text(
            '{"timestamp":1,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"one"}}}}\n'
            + '{"timestamp":2,"method":"session/update","params":{"update":{'
            '"sessionUpdate":"agent_message_chunk","content":{"type":"text",'
            '"text":"before <tool_call>{\\"name\\":\\"x\\"}</tool_call> after"}}}}\n'
        )

        messages = parse_grok(transcript, after_timestamp="1")

        assert len(messages) == 1
        assert messages[0].content == "before after [tool call details omitted for observation]"


class TestOpenCodeParser:
    def test_parse_plugin_message_events(self, tmp_path):
        from observational_memory.transcripts.opencode import parse_transcript

        transcript = tmp_path / "opencode.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "event": {
                        "type": "message.updated",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "ship OpenCode support"}],
                            "time": "2026-06-14T00:00:00Z",
                        },
                    }
                }
            )
            + "\n"
            + json.dumps(
                {
                    "event": {
                        "type": "message.updated",
                        "message": {"role": "assistant", "content": "I will add an OpenCode plugin."},
                    }
                }
            )
            + "\n"
            + json.dumps({"event": {"type": "session.idle"}})
            + "\n"
        )

        messages = parse_transcript(transcript)
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == "ship OpenCode support"
        assert messages[1].content == "I will add an OpenCode plugin."
        assert all(m.source == "opencode" for m in messages)
