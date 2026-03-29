"""Tests for om doctor enterprise provider checks."""

import json
import os
import subprocess

from click.testing import CliRunner

from observational_memory.cli import cli
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

    stop_check = _get_check(data, "Codex Stop hook")
    assert stop_check is not None
    assert stop_check["status"] == "WARN"

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
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{om_bin} codex-checkpoint",
                                    "statusMessage": "Checkpointing observational memory...",
                                }
                            ]
                        }
                    ],
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

    stop_check = _get_check(data, "Codex Stop hook")
    assert stop_check is not None
    assert stop_check["status"] == "PASS"

    agents_check = _get_check(data, "Codex AGENTS fallback")
    assert agents_check is not None
    assert agents_check["status"] == "PASS"

    command_check = _get_check(data, "Codex hook commands valid")
    assert command_check is not None
    assert command_check["status"] == "PASS"


def test_doctor_reports_launchd_and_legacy_cron_on_macos(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.codex_observe_launchd_plist_path.write_text("codex")
    config.auto_memory_launchd_plist_path.write_text("auto")
    config.reflect_launchd_plist_path.write_text("reflect")

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "print"]:
            return Result(returncode=0, stdout="service = loaded")
        if args == ["crontab", "-l"]:
            return Result(
                returncode=0,
                stdout=(
                    "# --- observational-memory ---\n"
                    "*/15 * * * * /tmp/bin/om observe --source codex 2>/dev/null\n"
                    "# --- end observational-memory ---\n"
                ),
            )
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert _get_check(data, "Scheduler default")["detail"] == "launchd"
    assert _get_check(data, "LaunchAgents")["status"] == "PASS"
    assert _get_check(data, "LaunchAgents loaded")["status"] == "PASS"
    assert _get_check(data, "Legacy cron jobs")["status"] == "WARN"


def test_doctor_warns_when_crontab_times_out(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    monkeypatch.setattr("observational_memory.cli.sys.platform", "linux")
    runner = CliRunner()

    def fake_run(args, **kwargs):
        if args == ["crontab", "-l"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    cron_check = _get_check(data, "Cron jobs")
    assert cron_check is not None
    assert cron_check["status"] == "WARN"
    assert "timed out after 5s" in cron_check["detail"]


def test_status_reports_duplicate_backstops_on_macos(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.ensure_memory_dir()
    config.codex_observe_launchd_plist_path.parent.mkdir(parents=True, exist_ok=True)
    config.codex_observe_launchd_plist_path.write_text("codex")
    config.auto_memory_launchd_plist_path.write_text("auto")
    config.reflect_launchd_plist_path.write_text("reflect")

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "print"]:
            return Result(returncode=0, stdout="service = loaded")
        if args == ["crontab", "-l"]:
            return Result(
                returncode=0,
                stdout=(
                    "# --- observational-memory ---\n"
                    "0 * * * * /tmp/bin/om observe --source claude-memory 2>/dev/null\n"
                    "# --- end observational-memory ---\n"
                ),
            )
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "Background scheduler:" in result.output
    assert "Default backend: launchd" in result.output
    assert "LaunchAgents: 3/3 installed" in result.output
    assert "Loaded: 3/3 loaded" in result.output
    assert "Cron jobs: 1 found (claude-memory)" in result.output
    assert "Duplicate backstops: launchd and cron are both present" in result.output
