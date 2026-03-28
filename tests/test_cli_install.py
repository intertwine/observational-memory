"""Tests for om install provider onboarding."""

import json
import tomllib
from pathlib import Path

from click.testing import CliRunner

from observational_memory.cli import _enable_codex_hooks_feature, cli
from observational_memory.config import Config


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    for p in (home, xdg_config, xdg_data, codex_home):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    for key in [
        "OM_LLM_PROVIDER",
        "OM_LLM_MODEL",
        "OM_LLM_OBSERVER_MODEL",
        "OM_LLM_REFLECTOR_MODEL",
        "OM_VERTEX_PROJECT_ID",
        "OM_VERTEX_REGION",
        "OM_BEDROCK_REGION",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_REGION",
    ]:
        monkeypatch.delenv(key, raising=False)

    return xdg_config / "observational-memory" / "env"


def test_install_non_interactive_vertex_requires_flags(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["install", "--codex", "--no-cron", "--provider", "anthropic-vertex", "--non-interactive"],
    )

    assert result.exit_code != 0
    assert "requires --vertex-project-id and --vertex-region" in result.output


def test_install_interactive_vertex_writes_expected_env(monkeypatch, tmp_path):
    env_file = _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # Prompt order: provider, model, vertex project, vertex region
    result = runner.invoke(
        cli,
        ["install", "--codex", "--no-cron"],
        input="anthropic-vertex\nclaude-enterprise\nproj-123\nus-east5\n",
    )

    assert result.exit_code == 0, result.output
    content = env_file.read_text()
    assert "OM_LLM_PROVIDER=anthropic-vertex" in content
    assert "OM_LLM_MODEL=claude-enterprise" in content
    assert "OM_VERTEX_PROJECT_ID=proj-123" in content
    assert "OM_VERTEX_REGION=us-east5" in content


def test_install_upserts_env_without_clobbering(monkeypatch, tmp_path):
    env_file = _set_base_env(monkeypatch, tmp_path)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("# keep-comment\nEXISTING_VAR=keep-me\n# OM_LLM_PROVIDER=auto\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--no-cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    content = env_file.read_text()
    assert "# keep-comment" in content
    assert "EXISTING_VAR=keep-me" in content
    assert "OM_LLM_PROVIDER=openai" in content
    assert "OM_LLM_MODEL=gpt-4o-mini" in content


def test_install_generates_compact_files_and_updates_codex_startup_integration(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    codex_agents = tmp_path / "codex" / "AGENTS.md"
    codex_agents.parent.mkdir(parents=True, exist_ok=True)
    codex_agents.write_text(
        "<!-- observational-memory -->\n"
        "1. `~/.local/share/observational-memory/reflections.md`\n"
        "2. `~/.local/share/observational-memory/observations.md`\n"
        "<!-- observational-memory -->\n"
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--no-cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    memory_dir = tmp_path / "data" / "observational-memory"
    assert (memory_dir / "profile.md").exists()
    assert (memory_dir / "active.md").exists()

    config_toml = (tmp_path / "codex" / "config.toml").read_text()
    assert "[features]" in config_toml
    assert "codex_hooks = true" in config_toml

    hooks_payload = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    session_start = hooks_payload["hooks"]["SessionStart"]
    om_groups = [
        group
        for group in session_start
        if group["hooks"][0].get("statusMessage") == "Loading observational memory..."
    ]
    assert om_groups
    assert om_groups[0]["matcher"] == "startup|resume"
    assert om_groups[0]["hooks"][0]["command"].endswith(" context")

    updated_agents = codex_agents.read_text()
    assert "<!-- observational-memory:codex-hooks-fallback-v1 -->" in updated_agents
    assert "Codex startup context is normally injected through hooks." in updated_agents
    assert "profile.md" in updated_agents
    assert "active.md" in updated_agents
    assert "om search" in updated_agents


def test_install_codex_preserves_existing_config_and_hooks(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    codex_home = tmp_path / "codex"
    config_toml = codex_home / "config.toml"
    config_toml.write_text('model = "gpt-5.4"\n\n[features]\nshell_snapshot = true\n')

    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing",
                                    "statusMessage": "Existing startup hook",
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo pre-tool"}],
                        }
                    ],
                }
            }
        )
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--no-cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output

    updated_config = config_toml.read_text()
    assert 'model = "gpt-5.4"' in updated_config
    assert "shell_snapshot = true" in updated_config
    assert "codex_hooks = true" in updated_config

    updated_hooks = json.loads(hooks_path.read_text())
    assert "PreToolUse" in updated_hooks["hooks"]
    session_start = updated_hooks["hooks"]["SessionStart"]
    assert any(group["hooks"][0].get("statusMessage") == "Existing startup hook" for group in session_start)
    assert any(group["hooks"][0].get("statusMessage") == "Loading observational memory..." for group in session_start)


def test_install_codex_preserves_dotted_features_syntax(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    codex_home = tmp_path / "codex"
    config_toml = codex_home / "config.toml"
    config_toml.write_text('model = "gpt-5.4"\nfeatures.shell_snapshot = true\n')

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--no-cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output

    updated_config = config_toml.read_text()
    assert "[features]" not in updated_config
    assert "features.shell_snapshot = true" in updated_config
    assert "features.codex_hooks = true" in updated_config

    parsed = tomllib.loads(updated_config)
    assert parsed["features"]["shell_snapshot"] is True
    assert parsed["features"]["codex_hooks"] is True


def test_enable_codex_hooks_feature_is_idempotent(monkeypatch, tmp_path, capsys):
    _set_base_env(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex"
    config_toml = codex_home / "config.toml"
    config_toml.write_text("[features]\ncodex_hooks = true\n")
    config = Config(codex_home=codex_home)

    original_write_text = Path.write_text
    write_calls: list[Path] = []

    def spy_write_text(self, data, *args, **kwargs):
        write_calls.append(self)
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)

    _enable_codex_hooks_feature(config)

    captured = capsys.readouterr()
    assert "already enabled" in captured.out
    assert config_toml.read_text() == "[features]\ncodex_hooks = true\n"
    assert config_toml not in write_calls


def test_uninstall_codex_removes_only_om_hook_and_agents_block(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()

    codex_home = tmp_path / "codex"
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing",
                                    "statusMessage": "Existing startup hook",
                                }
                            ],
                        },
                        {
                            "matcher": "startup|resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "om context",
                                    "statusMessage": "Loading observational memory...",
                                }
                            ],
                        },
                    ]
                }
            }
        )
    )

    codex_agents = codex_home / "AGENTS.md"
    codex_agents.write_text(
        "Team instructions\n\n"
        "<!-- observational-memory -->\n"
        "Codex startup context is normally injected through hooks.\n"
        "<!-- observational-memory -->\n"
    )

    result = runner.invoke(cli, ["uninstall", "--codex"])

    assert result.exit_code == 0, result.output
    updated_hooks = json.loads(hooks_path.read_text())
    session_start = updated_hooks["hooks"]["SessionStart"]
    assert len(session_start) == 1
    assert session_start[0]["hooks"][0]["statusMessage"] == "Existing startup hook"

    updated_agents = codex_agents.read_text()
    assert "Team instructions" in updated_agents
    assert "<!-- observational-memory -->" not in updated_agents
