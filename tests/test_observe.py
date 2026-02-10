"""Tests for the observer module."""

from pathlib import Path
from unittest.mock import patch

from observational_memory.config import Config
from observational_memory.observe import (
    run_observer,
    run_observer_backfill,
    _format_messages,
    _chunk_messages,
    _append_observations,
)
from observational_memory.transcripts import Message


def _sample_messages() -> list[Message]:
    return [
        Message(role="user", content="Help me set up PostgreSQL for my project Atlas.", timestamp="2026-02-10T14:00:00Z", source="claude"),
        Message(role="assistant", content="Sure! Let me help with PostgreSQL setup.", timestamp="2026-02-10T14:00:05Z", source="claude"),
        Message(role="user", content="I prefer Postgres over SQLite for production.", timestamp="2026-02-10T14:05:00Z", source="claude"),
        Message(role="assistant", content="Good choice. PostgreSQL handles concurrency much better.", timestamp="2026-02-10T14:05:05Z", source="claude"),
        Message(role="user", content="My name is Alex, I'm a backend engineer.", timestamp="2026-02-10T14:10:00Z", source="claude"),
        Message(role="assistant", content="Nice to meet you, Alex!", timestamp="2026-02-10T14:10:05Z", source="claude"),
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
