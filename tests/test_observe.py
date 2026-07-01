"""Tests for the observer module."""

import json
from pathlib import Path
from unittest.mock import PropertyMock, patch

from observational_memory.config import Config
from observational_memory.observe import (
    _append_observations,
    _chunk_messages,
    _codex_messages_since_cursor,
    _format_messages,
    _write_observations,
    observe_all_hermes,
    observe_codex_transcript,
    observe_grok_transcript,
    observe_hermes_transcript,
    run_observer,
    run_observer_backfill,
)
from observational_memory.transcripts import Message

FIXTURES = Path(__file__).parent / "fixtures"


def _sample_messages() -> list[Message]:
    return [
        Message(
            role="user",
            content="Help me set up PostgreSQL for my project Atlas.",
            timestamp="2026-02-10T14:00:00Z",
            source="claude",
        ),
        Message(
            role="assistant",
            content="Sure! Let me help with PostgreSQL setup.",
            timestamp="2026-02-10T14:00:05Z",
            source="claude",
        ),
        Message(
            role="user",
            content="I prefer Postgres over SQLite for production.",
            timestamp="2026-02-10T14:05:00Z",
            source="claude",
        ),
        Message(
            role="assistant",
            content="Good choice. PostgreSQL handles concurrency much better.",
            timestamp="2026-02-10T14:05:05Z",
            source="claude",
        ),
        Message(
            role="user",
            content="My name is Alex, I'm a backend engineer.",
            timestamp="2026-02-10T14:10:00Z",
            source="claude",
        ),
        Message(
            role="assistant",
            content="Nice to meet you, Alex!",
            timestamp="2026-02-10T14:10:05Z",
            source="claude",
        ),
    ]


class TestFormatMessages:
    def test_format_includes_timestamps(self):
        messages = _sample_messages()
        text = _format_messages(messages)
        assert "2026-02-10T14:00:00" in text

    def test_format_includes_roles(self):
        messages = _sample_messages()
        text = _format_messages(messages)
        assert "USER" in text
        assert "ASSISTANT" in text

    def test_format_includes_source(self):
        messages = _sample_messages()
        text = _format_messages(messages)
        assert "[claude]" in text


class TestRunObserver:
    def test_below_threshold_returns_none(self):
        config = Config(min_messages=10)
        messages = _sample_messages()[:2]
        result = run_observer(messages, config, dry_run=True)
        assert result is None

    @patch("observational_memory.observe.compress")
    def test_calls_llm_above_threshold(self, mock_compress):
        mock_compress.return_value = "# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test observation"

        config = Config(min_messages=3)
        messages = _sample_messages()
        result = run_observer(messages, config, dry_run=True)

        assert result is not None
        assert mock_compress.called

    @patch("observational_memory.observe.compress")
    def test_dry_run_does_not_write(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test"

        config = Config(memory_dir=tmp_path / "memory", min_messages=3)
        messages = _sample_messages()
        run_observer(messages, config, dry_run=True)

        assert not config.observations_path.exists()

    @patch("observational_memory.observe.compress")
    def test_writes_observations_file(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test"

        config = Config(memory_dir=tmp_path / "memory", min_messages=3)
        messages = _sample_messages()
        run_observer(messages, config, dry_run=False)

        assert config.observations_path.exists()
        content = config.observations_path.read_text()
        assert "Test" in content


class TestChunkMessages:
    def test_chunk_single_chunk(self):
        messages = _sample_messages()  # 6 messages
        chunks = _chunk_messages(messages, chunk_size=200)
        assert len(chunks) == 1
        assert len(chunks[0]) == 6

    def test_chunk_multiple_chunks(self):
        messages = _sample_messages() + _sample_messages()  # 12 messages
        chunks = _chunk_messages(messages, chunk_size=5)
        assert len(chunks) == 3  # 5, 5, 2
        assert len(chunks[0]) == 5
        assert len(chunks[1]) == 5
        assert len(chunks[2]) == 2

    def test_chunk_empty(self):
        chunks = _chunk_messages([], chunk_size=200)
        assert chunks == []

    def test_chunk_exact_multiple(self):
        messages = _sample_messages()  # 6 messages
        chunks = _chunk_messages(messages, chunk_size=3)
        assert len(chunks) == 2
        assert len(chunks[0]) == 3
        assert len(chunks[1]) == 3


class TestBackfillObserver:
    @patch("observational_memory.observe.compress")
    def test_backfill_does_not_include_existing_observations(self, mock_compress, tmp_path):
        mock_compress.return_value = "## 2026-02-10\n\n- 🔴 14:00 Backfill test"

        config = Config(memory_dir=tmp_path / "memory", min_messages=3)
        # Write existing observations that should NOT be sent to the LLM
        config.ensure_memory_dir()
        config.observations_path.write_text("## 2026-02-09\n\n- 🔴 Old observation\n")

        messages = _sample_messages()
        run_observer_backfill(messages, config, dry_run=True)

        # Check the user_content passed to compress does NOT contain existing observations
        call_args = mock_compress.call_args
        user_content = call_args[0][1]  # second positional arg
        assert "Existing observations" not in user_content
        assert "Old observation" not in user_content
        assert "New transcript to process" in user_content

    @patch("observational_memory.observe.compress")
    def test_backfill_appends_to_file(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", min_messages=3)
        config.ensure_memory_dir()
        config.observations_path.write_text("# Existing content\n")

        messages = _sample_messages()

        # First backfill run
        mock_compress.return_value = "## 2026-02-08\n\n- 🔴 First backfill"
        run_observer_backfill(messages, config, dry_run=False)

        # Second backfill run
        mock_compress.return_value = "## 2026-02-09\n\n- 🔴 Second backfill"
        run_observer_backfill(messages, config, dry_run=False)

        content = config.observations_path.read_text()
        assert "Existing content" in content
        assert "First backfill" in content
        assert "Second backfill" in content

    @patch("observational_memory.observe.compress")
    def test_backfill_below_threshold_returns_none(self, mock_compress):
        config = Config(min_messages=10)
        messages = _sample_messages()[:2]
        result = run_observer_backfill(messages, config, dry_run=True)
        assert result is None
        assert not mock_compress.called

    def test_append_observations_creates_file(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        _append_observations("## Test\n\n- observation", config)
        assert config.observations_path.exists()
        assert "observation" in config.observations_path.read_text()

    def test_append_observations_preserves_existing(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Header\n\nExisting\n")

        _append_observations("## New section", config)

        content = config.observations_path.read_text()
        assert "Existing" in content
        assert "New section" in content

    def test_append_observations_uses_atomic_write(self, monkeypatch, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        calls = []

        def fake_atomic_write(path, text, mode=None):
            calls.append((path, text, mode))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        monkeypatch.setattr("observational_memory.sync.atomic.atomic_write_text", fake_atomic_write)
        monkeypatch.setattr("observational_memory.startup_memory.refresh_startup_memory", lambda config: None)
        monkeypatch.setattr("observational_memory.observe._reindex_if_enabled", lambda config: None)

        _append_observations("## New section", config)

        assert calls == [(config.observations_path, "## New section\n", None)]

    def test_write_observations_uses_atomic_write(self, monkeypatch, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        calls = []

        def fake_atomic_write(path, text, mode=None):
            calls.append((path, text, mode))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        monkeypatch.setattr("observational_memory.sync.atomic.atomic_write_text", fake_atomic_write)
        monkeypatch.setattr("observational_memory.startup_memory.refresh_startup_memory", lambda config: None)
        monkeypatch.setattr("observational_memory.observe._reindex_if_enabled", lambda config: None)

        _write_observations("## New section", config)

        assert calls == [(config.observations_path, "## New section\n", None)]


class TestCodexObserver:
    @patch("observational_memory.observe.run_observer")
    def test_observe_codex_transcript_updates_cursor_by_message_count(self, mock_run_observer, tmp_path):
        mock_run_observer.return_value = "## 2026-02-10\n\n- checkpoint"

        config = Config(memory_dir=tmp_path / "memory")
        transcript = FIXTURES / "codex-transcript.jsonl"

        result = observe_codex_transcript(transcript, config, dry_run=False)

        assert result == "## 2026-02-10\n\n- checkpoint"
        assert config.load_cursor()[str(transcript)] == 7

    def test_codex_messages_since_cursor_migrates_legacy_line_offsets(self):
        transcript = FIXTURES / "codex-transcript.jsonl"

        messages, total = _codex_messages_since_cursor(transcript, {str(transcript): 3})

        assert total == 7
        assert len(messages) == 4
        assert messages[0].content.startswith("I'm using us-west-2")


class TestHermesObserver:
    @patch("observational_memory.observe.run_observer")
    def test_observe_hermes_transcript_passes_after_index_to_parser(self, mock_run_observer, monkeypatch, tmp_path):
        mock_run_observer.return_value = "## 2026-04-04\n\n- checkpoint"

        transcript = tmp_path / "hermes-session.jsonl"
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.save_cursor({str(transcript): 1})

        seen = {}

        def fake_parse(path, after_index=None):
            seen["path"] = path
            seen["after_index"] = after_index
            return [
                Message(role="assistant", content="two", timestamp="2026-04-04T00:00:01Z", source="hermes"),
                Message(role="user", content="three", timestamp="2026-04-04T00:00:02Z", source="hermes"),
            ]

        monkeypatch.setattr("observational_memory.transcripts.hermes.parse_transcript", fake_parse)

        result = observe_hermes_transcript(transcript, config, dry_run=False)

        assert result == "## 2026-04-04\n\n- checkpoint"
        assert seen == {"path": transcript, "after_index": 1}
        assert config.load_cursor()[str(transcript)] == 3

    @patch("observational_memory.observe.observe_hermes_transcript")
    def test_observe_all_hermes_uses_config_sessions_dir(self, mock_observe, tmp_path):
        sessions_dir = tmp_path / "custom-hermes"
        sessions_dir.mkdir(parents=True)
        transcript = sessions_dir / "session.jsonl"
        transcript.write_text(FIXTURES.joinpath("hermes-session.jsonl").read_text())
        config = Config(memory_dir=tmp_path / "memory")

        with patch.object(Config, "hermes_sessions_dir", new_callable=PropertyMock, return_value=sessions_dir):
            mock_observe.return_value = "## 2026-04-04\n\n- hermes"

            results = observe_all_hermes(config=config, dry_run=True)

        assert results == ["## 2026-04-04\n\n- hermes"]
        mock_observe.assert_called_once_with(transcript, config, True)


class TestGrokObserver:
    @staticmethod
    def _write_grok_messages(transcript: Path, count: int) -> None:
        transcript.write_text(
            "\n".join(
                json.dumps(
                    {
                        "timestamp": 1778885590 + i,
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": f"message-{i}"},
                            }
                        },
                    }
                )
                for i in range(count)
            )
            + "\n"
        )

    @patch("observational_memory.observe.run_observer")
    def test_observe_grok_transcript_advances_cursor_to_total_message_count(self, mock_run_observer, tmp_path):
        mock_run_observer.return_value = "## 2026-05-16\n\n- checkpoint"

        transcript = tmp_path / "updates.jsonl"
        config = Config(memory_dir=tmp_path / "memory", env_file=tmp_path / "config" / "env")

        self._write_grok_messages(transcript, 5)
        observe_grok_transcript(transcript, config, dry_run=False)
        assert config.load_cursor()[str(transcript)] == 5

        self._write_grok_messages(transcript, 7)
        observe_grok_transcript(transcript, config, dry_run=False)

        assert config.load_cursor()[str(transcript)] == 7
        assert mock_run_observer.call_args_list[1].args[0][0].content == "message-5"

    @patch("observational_memory.observe.run_observer")
    def test_observe_grok_transcript_advances_cursor_when_observer_returns_none(self, mock_run_observer, tmp_path):
        mock_run_observer.return_value = None

        transcript = tmp_path / "updates.jsonl"
        config = Config(memory_dir=tmp_path / "memory", env_file=tmp_path / "config" / "env", min_messages=5)

        self._write_grok_messages(transcript, 5)

        result = observe_grok_transcript(transcript, config, dry_run=False)

        assert result is None
        assert config.load_cursor()[str(transcript)] == 5


@patch("observational_memory.observe.run_observer")
def test_observe_kimi_transcript_parses_hook_events(mock_run_observer, tmp_path):
    from observational_memory.observe import observe_kimi_transcript

    transcript = tmp_path / "kimi-events.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "build Kimi support",
                        "om_captured_at": "2026-06-14T00:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "hook_event_name": "SubagentStop",
                        "agent_name": "coder",
                        "response": "done",
                        "om_captured_at": "2026-06-14T00:00:01Z",
                    }
                ),
            ]
        )
        + "\n"
    )
    config = Config(memory_dir=tmp_path / "memory")
    config.min_messages = 1
    mock_run_observer.return_value = "## 2026-06-14\n\n- kimi"

    result = observe_kimi_transcript(transcript, config, dry_run=False)

    assert result == "## 2026-06-14\n\n- kimi"
    messages = mock_run_observer.call_args.args[0]
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].source == "kimi"


@patch("observational_memory.observe.run_observer")
def test_observe_kimi_transcript_cursor_stops_at_processed_boundary(mock_run_observer, tmp_path):
    from observational_memory.observe import observe_kimi_transcript

    transcript = tmp_path / "kimi-events.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "first prompt",
                        "om_captured_at": "2026-06-14T00:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "hook_event_name": "SubagentStop",
                        "agent_name": "coder",
                        "response": "first response",
                        "om_captured_at": "2026-06-14T00:00:01Z",
                    }
                ),
            ]
        )
        + "\n"
    )
    config = Config(memory_dir=tmp_path / "memory")
    config.min_messages = 1

    def append_during_observation(*_args, **_kwargs):
        with transcript.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "second prompt",
                        "om_captured_at": "2026-06-14T00:00:02Z",
                    }
                )
                + "\n"
            )
        return "## 2026-06-14\n\n- kimi"

    mock_run_observer.side_effect = append_during_observation

    result = observe_kimi_transcript(transcript, config, dry_run=False)

    assert result == "## 2026-06-14\n\n- kimi"
    assert config.load_cursor()[str(transcript)] == 2
