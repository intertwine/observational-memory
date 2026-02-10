"""Tests for the config module."""

import os

from observational_memory.config import Config


class TestEnvFile:
    def test_ensure_env_file_creates_file(self, tmp_path):
        config = Config(env_file=tmp_path / "om" / "env")
        assert config.ensure_env_file() is True
        assert config.env_file.exists()
        # Check permissions (owner-only)
        assert oct(config.env_file.stat().st_mode & 0o777) == "0o600"

    def test_ensure_env_file_idempotent(self, tmp_path):
        config = Config(env_file=tmp_path / "om" / "env")
        config.ensure_env_file()
        assert config.ensure_env_file() is False  # already exists

    def test_load_env_file_sets_vars(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env"
        env_file.write_text("TEST_OM_KEY=secret123\n")
        monkeypatch.delenv("TEST_OM_KEY", raising=False)

        config = Config(env_file=env_file)
        config.load_env_file()

        assert os.environ.get("TEST_OM_KEY") == "secret123"
        # Clean up
        monkeypatch.delenv("TEST_OM_KEY", raising=False)

    def test_load_env_file_skips_comments(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env"
        env_file.write_text("# SHOULD_NOT_SET=value\nACTUAL_KEY=works\n")
        monkeypatch.delenv("SHOULD_NOT_SET", raising=False)
        monkeypatch.delenv("ACTUAL_KEY", raising=False)

        config = Config(env_file=env_file)
        config.load_env_file()

        assert os.environ.get("SHOULD_NOT_SET") is None
        assert os.environ.get("ACTUAL_KEY") == "works"
        monkeypatch.delenv("ACTUAL_KEY", raising=False)

    def test_load_env_file_does_not_overwrite(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env"
        env_file.write_text("EXISTING_VAR=from_file\n")
        monkeypatch.setenv("EXISTING_VAR", "from_env")

        config = Config(env_file=env_file)
        config.load_env_file()

        assert os.environ.get("EXISTING_VAR") == "from_env"

    def test_load_env_file_strips_quotes(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env"
        env_file.write_text("QUOTED_KEY='with-single-quotes'\nDOUBLE_KEY=\"with-double\"\n")
        monkeypatch.delenv("QUOTED_KEY", raising=False)
        monkeypatch.delenv("DOUBLE_KEY", raising=False)

        config = Config(env_file=env_file)
        config.load_env_file()

        assert os.environ.get("QUOTED_KEY") == "with-single-quotes"
        assert os.environ.get("DOUBLE_KEY") == "with-double"
        monkeypatch.delenv("QUOTED_KEY", raising=False)
        monkeypatch.delenv("DOUBLE_KEY", raising=False)

    def test_load_missing_env_file_is_noop(self, tmp_path):
        config = Config(env_file=tmp_path / "nonexistent")
        config.load_env_file()  # should not raise


class TestDetectProvider:
    def test_detects_anthropic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config(env_file=tmp_path / "env")
        assert config.detect_provider() == "anthropic"

    def test_detects_openai(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = Config(env_file=tmp_path / "env")
        assert config.detect_provider() == "openai"

    def test_prefers_anthropic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config = Config(env_file=tmp_path / "env")
        assert config.detect_provider() == "anthropic"

    def test_raises_without_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config(env_file=tmp_path / "env")
        try:
            config.detect_provider()
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "env" in str(e).lower()
