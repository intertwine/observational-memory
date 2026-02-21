"""Tests for the config module."""

import os

import pytest

from observational_memory.config import Config


@pytest.fixture(autouse=True)
def clear_llm_env(monkeypatch):
    for key in [
        "OM_LLM_PROVIDER",
        "OM_LLM_MODEL",
        "OM_LLM_OBSERVER_MODEL",
        "OM_LLM_REFLECTOR_MODEL",
        "OM_ANTHROPIC_MODEL",
        "OM_OPENAI_MODEL",
        "OM_VERTEX_PROJECT_ID",
        "OM_VERTEX_REGION",
        "OM_BEDROCK_REGION",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_REGION",
    ]:
        monkeypatch.delenv(key, raising=False)


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
            assert "provider" in str(e).lower()

    def test_explicit_vertex_provider(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config(env_file=tmp_path / "env", llm_provider="anthropic-vertex")
        assert config.resolve_provider() == "anthropic-vertex"

    def test_explicit_bedrock_provider(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config(env_file=tmp_path / "env", llm_provider="anthropic-bedrock")
        assert config.resolve_provider() == "anthropic-bedrock"

    def test_invalid_provider_value(self, tmp_path):
        config = Config(env_file=tmp_path / "env", llm_provider="bad-provider")
        try:
            config.resolve_provider()
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "invalid om_llm_provider" in str(e).lower()


class TestModelResolution:
    def test_operation_override_takes_precedence(self, tmp_path):
        config = Config(
            env_file=tmp_path / "env",
            llm_provider="anthropic",
            llm_model="shared-model",
            llm_observer_model="observer-model",
            llm_reflector_model="reflector-model",
        )
        assert config.resolve_model("observer", provider="anthropic") == "observer-model"
        assert config.resolve_model("reflector", provider="anthropic") == "reflector-model"

    def test_shared_model_used_when_operation_override_missing(self, tmp_path):
        config = Config(env_file=tmp_path / "env", llm_provider="anthropic", llm_model="shared-model")
        assert config.resolve_model("observer", provider="anthropic") == "shared-model"
        assert config.resolve_model("reflector", provider="anthropic") == "shared-model"

    def test_provider_defaults_when_no_shared_model(self, tmp_path):
        config = Config(env_file=tmp_path / "env", llm_provider="openai")
        assert config.resolve_model("observer", provider="openai") == "gpt-4o-mini"
        config2 = Config(env_file=tmp_path / "env", llm_provider="anthropic-bedrock")
        assert config2.resolve_model("observer", provider="anthropic-bedrock") == "claude-sonnet-4-5-20250929"


class TestProviderValidation:
    def test_vertex_missing_required_values(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OM_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("OM_VERTEX_REGION", raising=False)
        config = Config(env_file=tmp_path / "env", llm_provider="anthropic-vertex")
        try:
            config.validate_provider_config()
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "om_vertex_project_id" in str(e).lower()

    def test_vertex_valid_config(self, tmp_path):
        config = Config(
            env_file=tmp_path / "env",
            llm_provider="anthropic-vertex",
            vertex_project_id="proj",
            vertex_region="us-east5",
        )
        assert config.validate_provider_config() == "anthropic-vertex"

    def test_bedrock_uses_aws_region(self, tmp_path):
        config = Config(env_file=tmp_path / "env", llm_provider="anthropic-bedrock", bedrock_region="us-east-1")
        assert config.validate_provider_config() == "anthropic-bedrock"
