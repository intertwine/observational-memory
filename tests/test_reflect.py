"""Tests for the reflector module."""

import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from observational_memory.config import Config
from observational_memory.reflect import (
    _cap_reflector_output,
    _chunk_observations,
    _extract_latest_observation_date,
    _filter_new_observations,
    _parse_last_reflected,
    _parse_last_updated,
    _reflect_chunked,
    _stamp_timestamps,
    _trim_old_observations,
    reflector_catchup_needed,
    run_reflector,
)


class TestRunReflector:
    def test_no_observations_returns_none(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=tmp_path / "projects")
        config.ensure_memory_dir()
        result = run_reflector(config, dry_run=True)
        assert result is None

    def test_empty_observations_returns_none(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=tmp_path / "projects")
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


class TestReflectorCatchupNeeded:
    def test_returns_false_without_observations(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()

        assert reflector_catchup_needed(config) is False

    def test_returns_true_when_observations_exist_but_reflections_do_not(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-03-10\n\n- 🔴 14:00 Test\n")

        assert reflector_catchup_needed(config) is True

    def test_returns_false_when_new_day_has_started_but_daily_window_is_not_overdue(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text(
            "# Observations\n\n## 2026-03-10\n\n- 🔴 14:00 Test\n\n## 2026-03-11\n\n- 🔴 09:00 Newer test\n"
        )
        config.reflections_path.write_text(
            "# Reflections\n\n*Last updated: 2026-03-10 09:00 UTC*\n*Last reflected: 2026-03-10*\n"
        )

        now_utc = datetime(2026, 3, 11, 8, 59, tzinfo=timezone.utc)
        assert reflector_catchup_needed(config, now_utc=now_utc) is False

    def test_returns_true_when_latest_observation_is_newer_and_reflection_is_overdue(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text(
            "# Observations\n\n## 2026-03-10\n\n- 🔴 14:00 Test\n\n## 2026-03-11\n\n- 🔴 09:00 Newer test\n"
        )
        config.reflections_path.write_text(
            "# Reflections\n\n*Last updated: 2026-03-10 09:00 UTC*\n*Last reflected: 2026-03-10*\n"
        )

        now_utc = datetime(2026, 3, 11, 9, 1, tzinfo=timezone.utc)
        assert reflector_catchup_needed(config, now_utc=now_utc) is True

    def test_returns_false_when_reflections_are_current(self, tmp_path):
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-03-10\n\n- 🔴 14:00 Test\n")
        config.reflections_path.write_text(
            "# Reflections\n\n*Last updated: 2026-03-10 20:00 UTC*\n*Last reflected: 2026-03-10*\n"
        )

        assert reflector_catchup_needed(config) is False


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
        reflections = "# Reflections\n\n*Last reflected: 2026-02-10 23:45 UTC*\n"
        assert _parse_last_reflected(reflections) == "2026-02-10"

    def test_returns_none_when_missing(self):
        reflections = "# Reflections\n\n*Last updated: 2026-02-10*\n"
        assert _parse_last_reflected(reflections) is None

    def test_returns_none_for_empty_string(self):
        assert _parse_last_reflected("") is None


class TestParseLastUpdated:
    def test_extracts_timestamp(self):
        reflections = "# Reflections\n\n*Last updated: 2026-03-10 09:00 UTC*\n"
        assert _parse_last_updated(reflections) == datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)

    def test_returns_none_for_never(self):
        reflections = "# Reflections\n\n*Last updated: never*\n"
        assert _parse_last_updated(reflections) is None

    def test_returns_none_for_invalid_value(self):
        reflections = "# Reflections\n\n*Last updated: someday maybe*\n"
        assert _parse_last_updated(reflections) is None


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
        obs = "# Observations\n\n## 2026-02-07\n\n- old\n\n## 2026-02-10\n\n- new\n\n## 2026-02-09\n\n- middle\n"
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
            "# Reflections\n\n*Last updated: 2026-02-09 10:00 UTC*\n*Last reflected: 2026-02-08*\n\n## Core Identity\n"
        )
        result = _stamp_timestamps(reflections, "2026-02-10 14:00 UTC", "2026-02-10")
        assert "*Last updated: 2026-02-10 14:00 UTC*" in result
        assert "*Last reflected: 2026-02-10*" in result
        assert "2026-02-09" not in result
        assert "2026-02-08" not in result

    def test_injects_reflected_when_only_updated_exists(self):
        reflections = "# Reflections\n\n*Last updated: 2026-02-09 10:00 UTC*\n\n## Core Identity\n"
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
        # Create observations that exceed the budget per chunk.
        # Standalone fallback budget is ~94.5k chars (45000 * 3.5 * 0.6); three
        # ~40k sections still force more than one chunk.
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
        # Use a small input ceiling so two ~25k sections force exactly 2 chunks.
        config = Config(memory_dir=tmp_path / "memory", reflector_max_input_tokens=12000)

        # Simulate two chunks: compress called twice, each time returns updated reflections
        mock_compress.side_effect = [
            "# Reflections\n\n*Last updated: now*\n\n## After chunk 1",
            "# Reflections\n\n*Last updated: now*\n\n## After chunk 2",
        ]

        # Build observations large enough to force 2 chunks.
        # Each section must exceed the per-call ceiling when combined with
        # system prompt + reflections, but each individual section must fit
        # in a single chunk so we get exactly 2 calls.
        big = "- 🔴 10:00 " + "x" * 25000 + "\n\n"
        observations = f"# Observations\n\n## 2026-02-07\n\n{big}## 2026-02-08\n\n{big}"

        result = _reflect_chunked("system prompt", "existing reflections", observations, config)

        assert mock_compress.call_count == 2
        assert "After chunk 2" in result

    @patch("observational_memory.reflect.compress")
    def test_intermediate_chunks_get_note(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", reflector_max_input_tokens=12000)

        mock_compress.side_effect = [
            "# Reflections after 1",
            "# Reflections after 2",
        ]

        big = "- 🔴 10:00 " + "x" * 25000 + "\n\n"
        observations = f"# Observations\n\n## 2026-02-07\n\n{big}## 2026-02-08\n\n{big}"

        _reflect_chunked("system prompt", "", observations, config)

        # First call (intermediate) should have the NOTE in system prompt
        first_call_system = mock_compress.call_args_list[0][0][0]
        assert "chunk 1 of 2" in first_call_system

        # Last call should NOT have the NOTE
        last_call_system = mock_compress.call_args_list[1][0][0]
        assert "chunk" not in last_call_system or "NOTE" not in last_call_system

    @patch("observational_memory.reflect.compress")
    def test_final_chunk_gets_auto_memory_cleanup_note_on_deletion(self, mock_compress, tmp_path):
        config = Config(memory_dir=tmp_path / "memory", reflector_max_input_tokens=12000)

        mock_compress.side_effect = [
            "# Reflections after 1",
            "# Reflections after 2",
        ]

        big = "- 🔴 10:00 " + "x" * 25000 + "\n\n"
        observations = f"# Observations\n\n## 2026-02-07\n\n{big}## 2026-02-08\n\n{big}"

        _reflect_chunked("system prompt", "", observations, config, auto_memory="", amem_changed=True)

        first_call_user = mock_compress.call_args_list[0][0][1]
        last_call_user = mock_compress.call_args_list[1][0][1]
        assert "All auto-memory files have been removed" not in first_call_user
        assert "All auto-memory files have been removed" in last_call_user


class TestRunReflectorTimestampIntegration:
    """Integration tests for timestamp filtering in the full run_reflector flow."""

    @patch("observational_memory.reflect.compress")
    def test_stamps_last_reflected_on_output(self, mock_compress, tmp_path):
        mock_compress.return_value = (
            "# Reflections\n\n*Last updated: 2026-02-10 00:00 UTC*\n\n## Core Identity\n- Name: Test"
        )
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- 🔴 14:00 Test obs\n")

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
        config = Config(memory_dir=tmp_path / "memory", claude_projects_dir=tmp_path / "projects")
        config.ensure_memory_dir()

        config.reflections_path.write_text("# Reflections\n\n*Last reflected: 2026-02-10*\n\n## Core Identity\n")
        # All observations are older than the reflected date
        config.observations_path.write_text(
            "# Observations\n\n## 2026-02-07\n\n- 🔴 14:00 Old\n\n## 2026-02-08\n\n- 🔴 10:00 Also old\n"
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
            "# Observations\n\n## 2026-02-07\n\n- 🔴 14:00 Day 1\n\n## 2026-02-10\n\n- 🔴 22:00 Day 4\n"
        )

        run_reflector(config, dry_run=True)

        user_content = mock_compress.call_args[0][1]
        assert "Day 1" in user_content
        assert "Day 4" in user_content


class TestCapReflectorOutput:
    def test_under_cap_is_untouched(self):
        text = "# Reflections\n\n## Core Identity\n- Name: Alex\n\n## Active Projects\n- Thing\n"
        assert _cap_reflector_output(text, 200000) == text

    def test_zero_disables_cap(self):
        text = "# Reflections\n\n## A\n" + "x" * 1000
        assert _cap_reflector_output(text, 0) == text

    def test_trims_at_section_boundary_never_mid_section(self):
        # Three sections; cap lands inside the third. The trim must drop the whole
        # third section, never leave it half-written.
        text = (
            "# Reflections\n\n"
            "## Core Identity\n" + ("a" * 100) + "\n\n"
            "## Active Projects\n" + ("b" * 100) + "\n\n"
            "## Life & Operations\n" + ("c" * 5000) + "\n"
        )
        # Cap so the post-marker budget lands a little past the start of the third
        # heading: the only "## " boundary at/under the budget is the one before
        # "## Life & Operations", so the trim keeps the first two sections whole and
        # drops the runaway third entirely.
        cap = text.index("## Life & Operations") + 200
        result = _cap_reflector_output(text, cap)

        assert "## Core Identity" in result
        assert "## Active Projects" in result
        assert "## Life & Operations" not in result  # whole runaway section dropped
        assert "ccccc" not in result  # no mid-section fragment
        assert "OM_REFLECTOR_OUTPUT_MAX_CHARS" in result  # truncation marker
        assert len(result) <= cap

    def test_no_section_boundary_fits_keeps_preamble_not_mid_section(self):
        # A runaway FIRST section: no "## " boundary fits under the cap. The trim
        # must fall back to the document preamble (title block) — never a
        # mid-section slice that would persist a half-written entry.
        text = "# Reflections\n\n## Core Identity\n" + ("a" * 5000) + "\n"
        cap = 500  # smaller than the first section
        result = _cap_reflector_output(text, cap)

        assert "aaaaa" not in result  # never a mid-section fragment
        assert "## Core Identity" not in result  # the unfinished section is dropped whole
        assert "# Reflections" in result  # the safe preamble is preserved
        assert "OM_REFLECTOR_OUTPUT_MAX_CHARS" in result  # truncation marker
        assert len(result) <= cap

    def test_no_boundary_and_oversized_preamble_emits_marker_only(self):
        # Even the preamble exceeds the cap: emit the marker only, never a
        # fragment of any section.
        text = "# Reflections " + ("z" * 5000) + "\n\n## A\n- ok\n"
        cap = 200
        result = _cap_reflector_output(text, cap)

        assert "zzzzz" not in result  # no preamble/section fragment
        assert "OM_REFLECTOR_OUTPUT_MAX_CHARS" in result  # marker only
        assert len(result) <= cap

    def test_warns_when_cap_fires(self, caplog):
        text = "# Reflections\n\n## A\n" + ("x" * 200) + "\n\n## B\n" + ("y" * 200) + "\n"
        with caplog.at_level(logging.WARNING, logger="observational_memory.reflect"):
            _cap_reflector_output(text, 250)
        assert any("OM_REFLECTOR_OUTPUT_MAX_CHARS" in r.message for r in caplog.records)

    def test_no_warning_when_under_cap(self, caplog):
        text = "# Reflections\n\n## A\n- ok\n"
        with caplog.at_level(logging.WARNING, logger="observational_memory.reflect"):
            _cap_reflector_output(text, 200000)
        assert not caplog.records


def _fake_openai_returning(text):
    """Fake `openai` SDK whose Responses.create streams a single completed event.

    Mirrors the streaming shape `_call_codex_responses` consumes so the reflect
    pipeline runs through the real openai-chatgpt (Codex) path - the path that
    can't honor max_output_tokens - with a controllable output payload.
    """

    class FakeResponses:
        def create(self, **_kwargs):
            done = SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    output_text=text,
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                ),
            )
            return iter([done])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    return SimpleNamespace(OpenAI=FakeOpenAI)


class TestReflectorOutputCapOnCodexPath:
    """The output cap must hold on the openai-chatgpt (Codex) Responses path,
    which rejects max_output_tokens - so the cap is purely post-call."""

    def _codex_config(self, tmp_path, monkeypatch, *, output_cap):
        monkeypatch.setenv("OM_USAGE_TRACKING", "0")
        monkeypatch.setenv("OM_LLM_REFLECTOR_PROVIDER", "openai-chatgpt")
        monkeypatch.setenv("OM_REFLECTOR_OUTPUT_MAX_CHARS", str(output_cap))
        # Subscription tokens + cloudflare headers are mocked so routing reaches
        # the real Responses caller without a live login.
        monkeypatch.setattr("observational_memory.config._has_subscription_tokens", lambda *a, **k: True)
        monkeypatch.setattr(
            "observational_memory.auth.resolve_runtime_credentials",
            lambda *a, **k: {"base_url": "https://x", "access_token": "t"},
        )
        monkeypatch.setattr("observational_memory.auth.openai_chatgpt.cloudflare_headers", lambda token: {})
        config = Config(memory_dir=tmp_path / "memory")
        config.ensure_memory_dir()
        config.observations_path.write_text("# Observations\n\n## 2026-02-10\n\n- \U0001f534 14:00 Test\n")
        return config

    def test_over_cap_codex_output_trimmed_at_section_boundary(self, tmp_path, monkeypatch, caplog):
        over_cap = (
            "# Reflections\n\n"
            "## Core Identity\n" + ("a" * 200) + "\n\n"
            "## Active Projects\n" + ("b" * 200) + "\n\n"
            "## Life & Operations\n" + ("c" * 5000) + "\n"
        )
        cap = over_cap.index("## Life & Operations") + 200
        config = self._codex_config(tmp_path, monkeypatch, output_cap=cap)
        monkeypatch.setitem(sys.modules, "openai", _fake_openai_returning(over_cap))

        with caplog.at_level(logging.WARNING, logger="observational_memory.reflect"):
            result = run_reflector(config, dry_run=True)

        assert result is not None
        assert "## Core Identity" in result
        assert "## Active Projects" in result
        assert "## Life & Operations" not in result  # runaway section dropped whole
        assert "ccccc" not in result  # never trimmed mid-section
        assert any("OM_REFLECTOR_OUTPUT_MAX_CHARS" in r.message for r in caplog.records)

    def test_under_cap_codex_output_untouched(self, tmp_path, monkeypatch, caplog):
        under_cap = "# Reflections\n\n## Core Identity\n- Name: Alex\n\n## Active Projects\n- Thing\n"
        config = self._codex_config(tmp_path, monkeypatch, output_cap=200000)
        monkeypatch.setitem(sys.modules, "openai", _fake_openai_returning(under_cap))

        with caplog.at_level(logging.WARNING, logger="observational_memory.reflect"):
            result = run_reflector(config, dry_run=True)

        assert result is not None
        assert "- Name: Alex" in result
        assert "- Thing" in result
        assert "OM_REFLECTOR_OUTPUT_MAX_CHARS" not in result  # no truncation marker
        assert not any("OM_REFLECTOR_OUTPUT_MAX_CHARS" in r.message for r in caplog.records)
