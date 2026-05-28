"""Tests for the config module."""

import os

import pytest

from observational_memory.config import Config


@pytest.fixture(autouse=True)
def clear_llm_env(monkeypatch):
    for key in [
        "OM_SEARCH_BACKEND",
        "OM_QMD_INDEX_NAME",
        "OM_QMD_NO_RERANK",
        "OM_QMD_EMBED_MODEL",
        "OM_QMD_RERANK_MODEL",
        "OM_QMD_GENERATE_MODEL",
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

    def test_codex_paths_live_under_codex_home(self, tmp_path):
        codex_home = tmp_path / "codex-home"
        memory_dir = tmp_path / "memory"
        config = Config(codex_home=codex_home, memory_dir=memory_dir)

        assert config.codex_agents_md == codex_home / "AGENTS.md"
        assert config.codex_config_path == codex_home / "config.toml"
        assert config.codex_hooks_path == codex_home / "hooks.json"
        assert config.codex_checkpoint_state_path == memory_dir / ".codex-checkpoint-state.json"
        assert config.codex_checkpoint_lock_dir == memory_dir / ".codex-checkpoint-locks"

    def test_reflector_budget_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OM_REFLECTOR_MAX_INPUT_TOKENS", raising=False)
        monkeypatch.delenv("OM_REFLECTOR_OBSERVATION_CHUNK_RATIO", raising=False)

        config = Config(env_file=tmp_path / "env")

        # The settled #65 default raises the input ceiling so the configured
        # 48000-char reflections cap is not silently clamped below it.
        assert config.reflector_max_input_tokens == 45000
        assert config.reflector_observation_chunk_ratio == 0.6

    def test_reflector_budget_reads_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OM_REFLECTOR_MAX_INPUT_TOKENS", "30000")
        monkeypatch.setenv("OM_REFLECTOR_OBSERVATION_CHUNK_RATIO", "0.5")

        config = Config(env_file=tmp_path / "env")

        assert config.reflector_max_input_tokens == 30000
        assert config.reflector_observation_chunk_ratio == 0.5

    def test_qmd_config_reads_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OM_SEARCH_BACKEND", "qmd-hybrid")
        monkeypatch.setenv("OM_QMD_INDEX_NAME", "om-review")
        monkeypatch.setenv("OM_QMD_NO_RERANK", "yes")
        monkeypatch.setenv("OM_QMD_EMBED_MODEL", "embed-model")
        monkeypatch.setenv("OM_QMD_RERANK_MODEL", "rerank-model")
        monkeypatch.setenv("OM_QMD_GENERATE_MODEL", "generate-model")

        config = Config(env_file=tmp_path / "env")

        assert config.search_backend == "qmd-hybrid"
        assert config.qmd_index_name == "om-review"
        assert config.qmd_no_rerank is True
        assert config.qmd_model_env() == {
            "QMD_EMBED_MODEL": "embed-model",
            "QMD_RERANK_MODEL": "rerank-model",
            "QMD_GENERATE_MODEL": "generate-model",
        }

    def test_launchd_paths_live_under_launch_agents_and_memory_dir(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))

        memory_dir = tmp_path / "memory"
        config = Config(memory_dir=memory_dir)

        assert config.launch_agents_dir == home / "Library" / "LaunchAgents"
        assert config.scheduler_log_dir == memory_dir / ".scheduler-logs"
        assert config.codex_observe_launchd_plist_path == (
            config.launch_agents_dir / f"{config.CODEX_OBSERVE_LAUNCHD_LABEL}.plist"
        )
        assert config.auto_memory_launchd_plist_path == (
            config.launch_agents_dir / f"{config.AUTO_MEMORY_LAUNCHD_LABEL}.plist"
        )
        assert config.reflect_launchd_plist_path == (config.launch_agents_dir / f"{config.REFLECT_LAUNCHD_LABEL}.plist")
        assert config.codex_observe_launchd_stdout_path == config.scheduler_log_dir / "codex-observe.out.log"
        assert config.reflect_launchd_stderr_path == config.scheduler_log_dir / "reflect.err.log"


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
