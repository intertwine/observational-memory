"""Tests for the reflector module."""

from unittest.mock import patch

from observational_memory.config import Config
from observational_memory.reflect import run_reflector, _trim_old_observations


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
