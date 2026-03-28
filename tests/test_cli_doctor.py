"""Tests for om doctor enterprise provider checks."""

import json
import os

from click.testing import CliRunner

from observational_memory.cli import cli


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
        "OM_VERTEX_PROJECT_ID",
        "OM_VERTEX_REGION",
        "OM_BEDROCK_REGION",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)


def _get_check(results, name):
    for row in results:
        if row["name"] == name:
            return row
    return None


def test_doctor_provider_fail_closed_no_fallback(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    check = _get_check(data, "LLM provider config")
    assert check is not None
    assert check["status"] == "FAIL"
    assert "OPENAI_API_KEY" in check["detail"]


def test_doctor_validate_key_uses_selected_provider(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    monkeypatch.setattr("observational_memory.cli._validate_llm_access", lambda config: "openai")

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--json", "--validate-key"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    provider_check = _get_check(data, "LLM provider config")
    assert provider_check is not None
    assert provider_check["status"] == "PASS"

    validate_check = _get_check(data, "Configured LLM access")
    assert validate_check is not None
    assert validate_check["status"] == "PASS"
    assert "openai" in validate_check["detail"]


def test_doctor_vertex_missing_settings(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "anthropic-vertex")
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    check = _get_check(data, "LLM provider config")
    assert check is not None
    assert check["status"] == "FAIL"
    assert "OM_VERTEX_PROJECT_ID" in check["detail"]


def test_doctor_codex_startup_warns_when_only_agents_fallback_present(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    codex_agents = tmp_path / "codex" / "AGENTS.md"
    codex_agents.write_text(
        "<!-- observational-memory -->\n"
        "<!-- observational-memory:codex-hooks-fallback-v1 -->\n"
        "Codex startup context is normally injected through hooks.\n"
        "<!-- observational-memory -->\n"
    )

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    feature_check = _get_check(data, "Codex hooks feature")
    assert feature_check is not None
    assert feature_check["status"] == "WARN"

    hook_check = _get_check(data, "Codex SessionStart hook")
    assert hook_check is not None
    assert hook_check["status"] == "WARN"

    agents_check = _get_check(data, "Codex AGENTS fallback")
    assert agents_check is not None
    assert agents_check["status"] == "PASS"


def test_doctor_codex_startup_passes_with_hooks_enabled(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    codex_home = tmp_path / "codex"
    (codex_home / "config.toml").write_text("[features]\ncodex_hooks = true\n")

    om_bin = tmp_path / "bin" / "om"
    om_bin.parent.mkdir(parents=True, exist_ok=True)
    om_bin.write_text("#!/bin/sh\nexit 0\n")
    om_bin.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{om_bin.parent}:{old_path}")

    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{om_bin} context",
                                    "statusMessage": "Loading observational memory...",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    (codex_home / "AGENTS.md").write_text(
        "<!-- observational-memory -->\n"
        "<!-- observational-memory:codex-hooks-fallback-v1 -->\n"
        "Codex startup context is normally injected through hooks.\n"
        "<!-- observational-memory -->\n"
    )

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    feature_check = _get_check(data, "Codex hooks feature")
    assert feature_check is not None
    assert feature_check["status"] == "PASS"

    hook_check = _get_check(data, "Codex SessionStart hook")
    assert hook_check is not None
    assert hook_check["status"] == "PASS"

    agents_check = _get_check(data, "Codex AGENTS fallback")
    assert agents_check is not None
    assert agents_check["status"] == "PASS"

    command_check = _get_check(data, "Codex hook commands valid")
    assert command_check is not None
    assert command_check["status"] == "PASS"
