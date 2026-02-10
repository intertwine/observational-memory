"""Tests for the observer module."""

from pathlib import Path
from unittest.mock import patch

from observational_memory.config import Config
from observational_memory.observe import run_observer, _format_messages
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
