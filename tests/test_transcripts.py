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


class TestAsideParser:
    """Tests for the Aside browser-agent transcript parser (messages.jsonl)."""

    SAMPLE = (
        '{"role":"system-message","content":"skill docs available","kind":"site_skill","timestamp":1782425628633}\n'
        '{"role":"user","content":"Resume the Aside-om integration and re-test access.","timestamp":1782425628579}\n'
        '{"role":"assistant","content":['
        '{"type":"thinking","thinking":"Internal reasoning that must never leak.","thinkingSignature":"sig"},'
        '{"type":"text","text":"I will read the project memory first."},'
        '{"type":"toolCall","id":"toolu_1","name":"read_file","arguments":{"path":"memory/projects/om.md"}}'
        '],"provider":"anthropic","model":"claude","responseId":"r1","timestamp":1782425630000}\n'
        '{"role":"toolResult","toolName":"read_file","toolCallId":"toolu_1",'
        '"content":[{"type":"text","text":"file body"}],"isError":false,"timestamp":1782425631000}\n'
        '{"role":"assistant","content":['
        '{"type":"text","text":"Sandbox access is restored; om 0.8.0 is reachable."},'
        '{"type":"toolCall","id":"t2","name":"bash","arguments":{"title":"check om version","command":"om --version"}}'
        '],"timestamp":1782425632000}\n'
        '{"role":"user","content":"Great, wire it up.","timestamp":1782425633000}\n'
    )

    def test_parse_aside_messages_jsonl(self, tmp_path):
        from observational_memory.transcripts.aside import parse_transcript as parse_aside

        transcript = tmp_path / "messages.jsonl"
        transcript.write_text(self.SAMPLE)

        messages = parse_aside(transcript)

        # system-message and toolResult records are skipped.
        assert len(messages) == 4
        assert all(m.source == "aside" for m in messages)
        assert {m.role for m in messages} == {"user", "assistant"}

        blob = "\n".join(m.content for m in messages)
        # thinking blocks are dropped (never leak internal reasoning).
        assert "Internal reasoning" not in blob
        # text blocks preserved.
        assert "read the project memory" in blob
        # tool calls summarized with real (lowercase) Aside tool names.
        assert "[read_file: memory/projects/om.md]" in blob
        assert "[bash: check om version]" in blob

    def test_timestamps_normalized_to_iso(self, tmp_path):
        from observational_memory.transcripts.aside import parse_transcript as parse_aside

        transcript = tmp_path / "messages.jsonl"
        transcript.write_text(self.SAMPLE)

        messages = parse_aside(transcript)
        # epoch-ms ints become ISO-8601 UTC strings.
        assert messages[0].timestamp.startswith("2026-")
        assert "T" in messages[0].timestamp
        assert messages[0].timestamp.endswith("+00:00")

    def test_handles_empty_and_invalid(self, tmp_path):
        from observational_memory.transcripts.aside import parse_transcript as parse_aside

        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert parse_aside(empty) == []

        bad = tmp_path / "bad.jsonl"
        bad.write_text("not json\n{also not}\n")
        assert parse_aside(bad) == []

        missing = tmp_path / "nope.jsonl"
        assert parse_aside(missing) == []

    def test_find_transcripts_discovers_sessions(self, tmp_path):
        from observational_memory.transcripts.aside import find_all_transcripts, find_recent_transcripts

        # Real Aside layout: <home>/u/<idx>/agents/<agent>/sessions/<date>_<id>/messages.jsonl
        s1 = tmp_path / "u" / "0" / "agents" / "main" / "sessions" / "2026-06-25_aaa"
        s1.mkdir(parents=True)
        (s1 / "messages.jsonl").write_text(self.SAMPLE)

        time.sleep(0.05)
        s2 = tmp_path / "u" / "0" / "agents" / "main" / "sessions" / "2026-06-26_bbb"
        s2.mkdir(parents=True)
        (s2 / "messages.jsonl").write_text(self.SAMPLE)

        all_t = find_all_transcripts(tmp_path)
        assert len(all_t) == 2
        # oldest-first
        assert all_t[0].parent.name == "2026-06-25_aaa"

        recent = find_recent_transcripts(tmp_path)
        assert len(recent) == 2
        # newest-first
        assert recent[0].parent.name == "2026-06-26_bbb"

    def test_find_transcripts_missing_home(self, tmp_path):
        from observational_memory.transcripts.aside import find_all_transcripts

        assert find_all_transcripts(tmp_path / "nonexistent") == []
