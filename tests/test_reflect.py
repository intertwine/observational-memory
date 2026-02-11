"""Tests for the reflector module."""

from unittest.mock import patch, call

from observational_memory.config import Config
from observational_memory.reflect import (
    run_reflector,
    _trim_old_observations,
    _parse_last_reflected,
    _filter_new_observations,
    _extract_latest_observation_date,
    _stamp_timestamps,
    _chunk_observations,
    _reflect_chunked,
)


class TestRunReflector:
    def test_no_observations_returns_none(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        result = run_reflector(config, dry_run=True)
        assert result is None

    def test_empty_observations_returns_none(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("")
        result = run_reflector(config, dry_run=True)
        assert result is None

    @patch("observational_memory.reflect.compress")
    def test_calls_llm_with_observations(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Reflections\n\n## Core Identity\n- Name: Alex"

        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test")

        result = run_reflector(config, dry_run=True)
        assert result is not None
        assert mock_compress.called

    @patch("observational_memory.reflect.compress")
    def test_dry_run_does_not_write(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Reflections\n\n## Core Identity\n- Name: Alex"

        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test")

        run_reflector(config, dry_run=True)
        assert not config.reflections_path.exists()

    @patch("observational_memory.reflect.compress")
    def test_writes_reflections_file(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Reflections\n\n## Core Identity\n- Name: Alex"

        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test")

        run_reflector(config, dry_run=False)
        assert config.reflections_path.exists()
        assert "Alex" in config.reflections_path.read_text()


class TestTrimObservations:
    def test_keeps_recent_observations(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", observation_retention_days=7)
        config.ensure_memory_dir()

        content = "# Observations\n\n## 2099-12-31\n\n- 🔴 14:00 Future observation\n"
        config.observations_path.write_text(content)

        _trim_old_observations(config)

        result = config.observations_path.read_text()
        assert "Future observation" in result

    def test_removes_old_observations(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", observation_retention_days=7)
        config.ensure_memory_dir()

        content = (
            "# Observations\n\n"
            "## 2020-01-01\n\n- 🔴 14:00 Very old observation\n\n"
            "## 2099-12-31\n\n- 🔴 14:00 Future observation\n"
        )
        config.observations_path.write_text(content)

        _trim_old_observations(config)

        result = config.observations_path.read_text()
        assert "Very old" not in result
        assert "Future observation" in result


class TestParseLastReflected:
    def test_extracts_date(self):
        reflections = (
            "# Reflections — Long-Term Memory\n\n"
            "*Last updated: 2026-02-10 14:00 UTC*\n"
            "*Last reflected: 2026-02-09*\n\n"
            "## Core Identity\n"
        )
        assert _parse_last_reflected(reflections) == "2026-02-09"

    def test_extracts_date_with_time(self):
        reflections = (
            "# Reflections\n\n"
            "*Last reflected: 2026-02-10 23:45 UTC*\n"
        )
        assert _parse_last_reflected(reflections) == "2026-02-10"

    def test_returns_none_when_missing(self):
        reflections = "# Reflections\n\n*Last updated: 2026-02-10*\n"
        assert _parse_last_reflected(reflections) is None

    def test_returns_none_for_empty_string(self):
        assert _parse_last_reflected("") is None


class TestFilterNewObservations:
    OBSERVATIONS = (
        "# Observations\n\n<!-- Auto-maintained. -->\n\n"
        "## 2026-02-07\n\n- 🔴 14:00 Old stuff\n\n"
        "## 2026-02-08\n\n- 🔴 10:00 Middle stuff\n\n"
        "## 2026-02-09\n\n- 🟡 09:00 Recent stuff\n\n"
        "## 2026-02-10\n\n- 🔴 22:00 Latest stuff\n"
    )

    def test_returns_all_when_since_date_none(self):
        result = _filter_new_observations(self.OBSERVATIONS, None)
        assert result == self.OBSERVATIONS

    def test_filters_from_date_inclusive(self):
        result = _filter_new_observations(self.OBSERVATIONS, "2026-02-09")
        assert "Old stuff" not in result
        assert "Middle stuff" not in result
        assert "Recent stuff" in result
        assert "Latest stuff" in result

    def test_preserves_header(self):
        result = _filter_new_observations(self.OBSERVATIONS, "2026-02-09")
        assert "# Observations" in result

    def test_returns_empty_when_all_older(self):
        result = _filter_new_observations(self.OBSERVATIONS, "2099-01-01")
        assert result == ""

    def test_returns_all_dates_when_since_very_old(self):
        result = _filter_new_observations(self.OBSERVATIONS, "2020-01-01")
        assert "Old stuff" in result
        assert "Latest stuff" in result

    def test_single_date_section(self):
        obs = "# Observations\n\n## 2026-02-10\n\n- 🔴 10:00 Only entry\n"
        result = _filter_new_observations(obs, "2026-02-10")
        assert "Only entry" in result

    def test_single_date_section_filtered_out(self):
        obs = "# Observations\n\n## 2026-02-10\n\n- 🔴 10:00 Only entry\n"
        result = _filter_new_observations(obs, "2026-02-11")
        assert result == ""


class TestExtractLatestObservationDate:
    def test_finds_latest(self):
        obs = (
            "# Observations\n\n"
            "## 2026-02-07\n\n- old\n\n"
            "## 2026-02-10\n\n- new\n\n"
            "## 2026-02-09\n\n- middle\n"
        )
        assert _extract_latest_observation_date(obs) == "2026-02-10"

    def test_single_date(self):
        obs = "## 2026-01-15\n\n- entry\n"
        assert _extract_latest_observation_date(obs) == "2026-01-15"

    def test_returns_none_when_no_dates(self):
        assert _extract_latest_observation_date("# Observations\n\nno dates here") is None

    def test_returns_none_for_empty(self):
        assert _extract_latest_observation_date("") is None


class TestStampTimestamps:
    def test_replaces_existing_timestamps(self):
        reflections = (
            "# Reflections\n\n"
            "*Last updated: 2026-02-09 10:00 UTC*\n"
            "*Last reflected: 2026-02-08*\n\n"
            "## Core Identity\n"
        )
        result = _stamp_timestamps(reflections, "2026-02-10 14:00 UTC", "2026-02-10")
        assert "*Last updated: 2026-02-10 14:00 UTC*" in result
        assert "*Last reflected: 2026-02-10*" in result
        assert "2026-02-09" not in result
        assert "2026-02-08" not in result

    def test_injects_reflected_when_only_updated_exists(self):
        reflections = (
            "# Reflections\n\n"
            "*Last updated: 2026-02-09 10:00 UTC*\n\n"
            "## Core Identity\n"
        )
        result = _stamp_timestamps(reflections, "2026-02-10 14:00 UTC", "2026-02-10")
        assert "*Last updated: 2026-02-10 14:00 UTC*" in result
        assert "*Last reflected: 2026-02-10*" in result

    def test_injects_both_when_neither_exists(self):
        reflections = "# Reflections\n\n## Core Identity\n"
        result = _stamp_timestamps(reflections, "2026-02-10 14:00 UTC", "2026-02-10")
        assert "*Last updated: 2026-02-10 14:00 UTC*" in result
        assert "*Last reflected: 2026-02-10*" in result

    def test_preserves_rest_of_document(self):
        reflections = (
            "# Reflections\n\n"
            "*Last updated: 2026-02-09 10:00 UTC*\n"
            "*Last reflected: 2026-02-08*\n\n"
            "## Core Identity\n- **Name:** Bryan\n"
        )
        result = _stamp_timestamps(reflections, "2026-02-10 14:00 UTC", "2026-02-10")
        assert "## Core Identity" in result
        assert "- **Name:** Bryan" in result


class TestChunkObservations:
    def test_single_small_section_returns_one_chunk(self):
        obs = "# Observations\n\n## 2026-02-10\n\n- 🔴 10:00 Small entry\n"
        chunks = _chunk_observations(obs)
        assert len(chunks) == 1
        assert "Small entry" in chunks[0]

    def test_preserves_header_in_chunks(self):
        obs = "# Observations\n\n## 2026-02-10\n\n- 🔴 10:00 Entry\n"
        chunks = _chunk_observations(obs)
        assert "# Observations" in chunks[0]

    def test_splits_large_observations(self):
        # Create observations that exceed the budget per chunk
        # Budget is ~63k chars (30000 * 3.5 * 0.6)
        big_section = "## 2026-02-0{i}\n\n" + "- 🔴 10:00 " + "x" * 40000 + "\n\n"
        obs = "# Observations\n\n"
        obs += big_section.format(i=1)
        obs += big_section.format(i=2)
        obs += big_section.format(i=3)

        chunks = _chunk_observations(obs)
        assert len(chunks) >= 2

    def test_no_date_sections_returns_single_chunk(self):
        obs = "# Observations\n\nJust some text without date headers\n"
        chunks = _chunk_observations(obs)
        assert len(chunks) == 1
        assert chunks[0] == obs

    def test_multiple_small_sections_fit_one_chunk(self):
        obs = (
            "# Observations\n\n"
            "## 2026-02-07\n\n- 🔴 10:00 Day 1\n\n"
            "## 2026-02-08\n\n- 🔴 10:00 Day 2\n\n"
            "## 2026-02-09\n\n- 🔴 10:00 Day 3\n"
        )
        chunks = _chunk_observations(obs)
        assert len(chunks) == 1
        assert "Day 1" in chunks[0]
        assert "Day 3" in chunks[0]


class TestReflectChunked:
    @patch("observational_memory.reflect.compress")
    def test_folds_chunks_sequentially(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")

        # Simulate two chunks: compress called twice, each time returns updated reflections
        mock_compress.side_effect = [
            "# Reflections\n\n*Last updated: now*\n\n## After chunk 1",
            "# Reflections\n\n*Last updated: now*\n\n## After chunk 2",
        ]

        # Build observations large enough to force 2 chunks
        big = "- 🔴 10:00 " + "x" * 50000 + "\n\n"
        observations = (
            "# Observations\n\n"
            f"## 2026-02-07\n\n{big}"
            f"## 2026-02-08\n\n{big}"
        )

        result = _reflect_chunked("system prompt", "existing reflections", observations, config)

        assert mock_compress.call_count == 2
        assert "After chunk 2" in result

    @patch("observational_memory.reflect.compress")
    def test_intermediate_chunks_get_note(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")

        mock_compress.side_effect = [
            "# Reflections after 1",
            "# Reflections after 2",
        ]

        big = "- 🔴 10:00 " + "x" * 50000 + "\n\n"
        observations = (
            "# Observations\n\n"
            f"## 2026-02-07\n\n{big}"
            f"## 2026-02-08\n\n{big}"
        )

        _reflect_chunked("system prompt", "", observations, config)

        # First call (intermediate) should have the NOTE in system prompt
        first_call_system = mock_compress.call_args_list[0][0][0]
        assert "chunk 1 of 2" in first_call_system

        # Last call should NOT have the NOTE
        last_call_system = mock_compress.call_args_list[1][0][0]
        assert "chunk" not in last_call_system or "NOTE" not in last_call_system


class TestRunReflectorTimestampIntegration:
    """Integration tests for timestamp filtering in the full run_reflector flow."""

    @patch("observational_memory.reflect.compress")
    def test_stamps_last_reflected_on_output(self, mock_compress, tmp_path):
        mock_compress.return_value = (
            "# Reflections\n\n"
            "*Last updated: 2026-02-10 00:00 UTC*\n\n"
            "## Core Identity\n- Name: Test"
        )
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text(
            "# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test obs\n"
        )

        result = run_reflector(config, dry_run=True)
        assert "*Last reflected: 2026-02-10*" in result

    @patch("observational_memory.reflect.compress")
    def test_filters_old_observations(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Reflections\n\n## Core Identity\n- Name: Test"

        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()

        # Existing reflections with Last reflected timestamp
        config.reflections_path.write_text(
            "# Reflections\n\n"
            "*Last updated: 2026-02-09 10:00 UTC*\n"
            "*Last reflected: 2026-02-09*\n\n"
            "## Core Identity\n- Name: Test\n"
        )
        config.observations_path.write_text(
            "# Observations\n\n"
            "## 2026-02-07\n\n- 🔴 14:00 Old obs\n\n"
            "## 2026-02-08\n\n- 🔴 10:00 Also old\n\n"
            "## 2026-02-09\n\n- 🟡 09:00 Same day\n\n"
            "## 2026-02-10\n\n- 🔴 22:00 New obs\n"
        )

        run_reflector(config, dry_run=True)

        # The user_content passed to compress should NOT contain old observations
        user_content = mock_compress.call_args[0][1]
        assert "Old obs" not in user_content
        assert "Also old" not in user_content
        # Should contain observations from the reflected date onward (inclusive)
        assert "Same day" in user_content
        assert "New obs" in user_content

    @patch("observational_memory.reflect.compress")
    def test_returns_none_when_no_new_observations(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()

        config.reflections_path.write_text(
            "# Reflections\n\n"
            "*Last reflected: 2026-02-10*\n\n"
            "## Core Identity\n"
        )
        # All observations are older than the reflected date
        config.observations_path.write_text(
            "# Observations\n\n"
            "## 2026-02-07\n\n- 🔴 14:00 Old\n\n"
            "## 2026-02-08\n\n- 🔴 10:00 Also old\n"
        )

        result = run_reflector(config, dry_run=True)
        assert result is None
        assert not mock_compress.called

    @patch("observational_memory.reflect.compress")
    def test_first_run_no_timestamp_processes_all(self, mock_compress, tmp_path):
        mock_compress.return_value = "# Reflections\n\n## Core Identity\n- Name: Test"

        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text(
            "# Observations\n\n"
            "## 2026-02-07\n\n- 🔴 14:00 Day 1\n\n"
            "## 2026-02-10\n\n- 🔴 22:00 Day 4\n"
        )

        run_reflector(config, dry_run=True)

        user_content = mock_compress.call_args[0][1]
        assert "Day 1" in user_content
        assert "Day 4" in user_content
