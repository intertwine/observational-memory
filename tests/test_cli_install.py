"""Tests for om install provider onboarding."""

import json
import os
import plistlib
import subprocess
import tomllib
from pathlib import Path

import click
from click.testing import CliRunner

from observational_memory.cli import (
    _codex_hooks_feature_enabled,
    _cron_job_keys_for_targets,
    _desired_cron_jobs,
    _enable_codex_hooks_feature,
    _launchd_job_specs,
    _resolve_scheduler_mode,
    _uninstall_cron,
    cli,
)
from observational_memory.config import Config


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    grok_home = tmp_path / "grok"
    for p in (home, xdg_config, xdg_data, codex_home, grok_home):
        p.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GROK_HOME", str(grok_home))

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
    assert "hooks = true" in config_toml
    assert "codex_hooks = true" in config_toml
    parsed_config = tomllib.loads(config_toml)
    assert parsed_config["features"]["hooks"] is True
    assert parsed_config["features"]["codex_hooks"] is True

    hooks_payload = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    session_start = hooks_payload["hooks"]["SessionStart"]
    om_groups = [
        group for group in session_start if group["hooks"][0].get("statusMessage") == "Loading observational memory..."
    ]
    assert om_groups
    assert om_groups[0]["matcher"] == "startup|resume"
    assert om_groups[0]["hooks"][0]["command"].endswith(" context")
    stop_groups = hooks_payload["hooks"]["Stop"]
    om_stop_groups = [
        group
        for group in stop_groups
        if group["hooks"][0].get("statusMessage") == "Checkpointing observational memory..."
    ]
    assert om_stop_groups
    assert om_stop_groups[0]["hooks"][0]["command"].endswith(" codex-checkpoint")

    updated_agents = codex_agents.read_text()
    assert "<!-- observational-memory:codex-hooks-fallback-v2 -->" in updated_agents
    assert "Codex startup context is normally injected through hooks." in updated_agents
    assert 'om context --for codex --cwd "$PWD"' in updated_agents
    assert "do not bulk-read generated memory files" in updated_agents
    assert 'om recall --query "<query>"' in updated_agents
    assert "read these files before substantial work" not in updated_agents
    assert "~/.local/share/observational-memory/reflections.md" not in updated_agents
    assert "~/.local/share/observational-memory/observations.md" not in updated_agents
    assert "om search" in updated_agents


def test_install_cowork_copies_valid_plugin(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "install",
            "--cowork",
            "--no-cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    plugin_dir = (
        tmp_path
        / "home"
        / "Library"
        / "Application Support"
        / "Claude"
        / "local-agent-mode-plugins"
        / "observational-memory"
    )
    hooks_json = plugin_dir / "hooks" / "hooks.json"
    hooks_payload = json.loads(hooks_json.read_text())
    assert set(hooks_payload["hooks"]) == {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact"}
    from observational_memory import __version__ as om_version

    # The Cowork plugin manifest tracks the package version (kept in sync by
    # scripts/bump_version.py); assert against it rather than a literal.
    assert json.loads((plugin_dir / "version.json").read_text()) == {"version": om_version}
    assert os.access(plugin_dir / "hooks" / "scripts" / "session-start.sh", os.X_OK)
    assert os.access(plugin_dir / "hooks" / "scripts" / "session-end.sh", os.X_OK)


def test_cowork_target_does_not_manage_scheduler_jobs(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setattr("observational_memory.cli._find_om_path", lambda: "/tmp/bin/om")
    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")

    assert _launchd_job_specs(config, "cowork", om_path="/tmp/bin/om") == []
    assert _cron_job_keys_for_targets("cowork") == set()
    assert _desired_cron_jobs(config, "cowork") == {}


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
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing-stop",
                                    "statusMessage": "Existing checkpoint hook",
                                }
                            ]
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
    assert "hooks = true" in updated_config
    assert "codex_hooks = true" in updated_config

    updated_hooks = json.loads(hooks_path.read_text())
    assert "PreToolUse" in updated_hooks["hooks"]
    session_start = updated_hooks["hooks"]["SessionStart"]
    assert any(group["hooks"][0].get("statusMessage") == "Existing startup hook" for group in session_start)
    assert any(group["hooks"][0].get("statusMessage") == "Loading observational memory..." for group in session_start)
    stop_groups = updated_hooks["hooks"]["Stop"]
    assert any(group["hooks"][0].get("statusMessage") == "Existing checkpoint hook" for group in stop_groups)
    assert any(
        group["hooks"][0].get("statusMessage") == "Checkpointing observational memory..." for group in stop_groups
    )


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
    assert "features.hooks = true" in updated_config
    assert "features.codex_hooks = true" in updated_config

    parsed = tomllib.loads(updated_config)
    assert parsed["features"]["shell_snapshot"] is True
    assert parsed["features"]["hooks"] is True
    assert parsed["features"]["codex_hooks"] is True


def test_enable_codex_hooks_feature_is_idempotent(monkeypatch, tmp_path, capsys):
    _set_base_env(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex"
    config_toml = codex_home / "config.toml"
    config_toml.write_text("[features]\nhooks = true\ncodex_hooks = true\n")
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
    assert config_toml.read_text() == "[features]\nhooks = true\ncodex_hooks = true\n"
    assert config_toml not in write_calls


def test_enable_codex_hooks_feature_migrates_legacy_flag(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex"
    config_toml = codex_home / "config.toml"
    config_toml.write_text("[features]\ncodex_hooks = true\n")
    config = Config(codex_home=codex_home)

    _enable_codex_hooks_feature(config)

    updated_config = config_toml.read_text()
    assert "hooks = true" in updated_config
    assert "codex_hooks = true" in updated_config
    parsed = tomllib.loads(updated_config)
    assert parsed["features"]["hooks"] is True
    assert parsed["features"]["codex_hooks"] is True


def test_codex_hooks_feature_enabled_accepts_canonical_and_legacy_flags(tmp_path):
    canonical_home = tmp_path / "canonical"
    canonical_home.mkdir()
    (canonical_home / "config.toml").write_text("[features]\nhooks = true\n")
    assert _codex_hooks_feature_enabled(Config(codex_home=canonical_home)) == (True, None)

    legacy_home = tmp_path / "legacy"
    legacy_home.mkdir()
    (legacy_home / "config.toml").write_text("[features]\ncodex_hooks = true\n")
    assert _codex_hooks_feature_enabled(Config(codex_home=legacy_home)) == (True, None)


def test_resolve_scheduler_mode_auto_uses_launchd_on_macos():
    assert _resolve_scheduler_mode("auto", None, platform="darwin") == "launchd"


def test_resolve_scheduler_mode_auto_uses_cron_on_non_macos():
    assert _resolve_scheduler_mode("auto", None, platform="linux") == "cron"


def test_resolve_scheduler_mode_respects_legacy_no_cron():
    assert _resolve_scheduler_mode("auto", False, platform="darwin") == "none"


def test_resolve_scheduler_mode_rejects_conflicting_legacy_flags():
    try:
        _resolve_scheduler_mode("launchd", False, platform="darwin")
        assert False, "Should have raised"
    except Exception as exc:
        assert "conflicts" in str(exc)


def test_install_auto_scheduler_installs_launchd_on_macos(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "observational_memory.cli._install_launchd",
        lambda config, targets: calls.append(("launchd", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._install_cron",
        lambda config, targets: calls.append(("cron", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_cron",
        lambda targets="both": calls.append(("uninstall-cron", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("launchd", "codex"), ("uninstall-cron", "codex")]


def test_install_explicit_launchd_writes_plists_and_bootstraps(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    monkeypatch.setattr("observational_memory.cli._find_om_path", lambda: "/tmp/bin/om")
    monkeypatch.setattr("observational_memory.cli._uninstall_cron", lambda targets="both": None)
    runner = CliRunner()

    subprocess_calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        subprocess_calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--scheduler",
            "launchd",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    plist_paths = [
        config.codex_observe_launchd_plist_path,
        config.auto_memory_launchd_plist_path,
        config.reflect_launchd_plist_path,
    ]
    for path in plist_paths:
        assert path.exists()

    codex_plist = plistlib.loads(config.codex_observe_launchd_plist_path.read_bytes())
    assert codex_plist["Label"] == config.CODEX_OBSERVE_LAUNCHD_LABEL
    assert codex_plist["ProgramArguments"] == ["/tmp/bin/om", "observe", "--source", "codex"]
    assert codex_plist["RunAtLoad"] is True
    assert codex_plist["StartInterval"] == 900
    assert codex_plist["StandardOutPath"] == str(config.codex_observe_launchd_stdout_path)
    assert codex_plist["StandardErrorPath"] == str(config.codex_observe_launchd_stderr_path)

    reflect_plist = plistlib.loads(config.reflect_launchd_plist_path.read_bytes())
    assert reflect_plist["Label"] == config.REFLECT_LAUNCHD_LABEL
    assert reflect_plist["ProgramArguments"] == ["/tmp/bin/om", "reflect"]
    assert reflect_plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert "RunAtLoad" not in reflect_plist

    expected_calls = [
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.CODEX_OBSERVE_LAUNCHD_LABEL}"],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(config.codex_observe_launchd_plist_path)],
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.AUTO_MEMORY_LAUNCHD_LABEL}"],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(config.auto_memory_launchd_plist_path)],
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.REFLECT_LAUNCHD_LABEL}"],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(config.reflect_launchd_plist_path)],
    ]
    assert [args for args, _ in subprocess_calls] == expected_calls


def test_install_legacy_cron_flag_selects_cron_scheduler(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "observational_memory.cli._install_launchd",
        lambda config, targets: calls.append(("launchd", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._install_cron",
        lambda config, targets: calls.append(("cron", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_launchd",
        lambda config, targets="both": calls.append(("uninstall-launchd", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("cron", "codex"), ("uninstall-launchd", "codex")]


def test_uninstall_codex_removes_only_om_hook_and_agents_block(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    monkeypatch.setattr("observational_memory.cli._uninstall_cron", lambda targets="both": None)

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
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing-stop",
                                    "statusMessage": "Existing checkpoint hook",
                                }
                            ],
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "om codex-checkpoint",
                                    "statusMessage": "Checkpointing observational memory...",
                                }
                            ],
                        },
                    ],
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
    stop_groups = updated_hooks["hooks"]["Stop"]
    assert len(stop_groups) == 1
    assert stop_groups[0]["hooks"][0]["statusMessage"] == "Existing checkpoint hook"

    updated_agents = codex_agents.read_text()
    assert "Team instructions" in updated_agents
    assert "<!-- observational-memory -->" not in updated_agents


def test_uninstall_on_macos_removes_om_launch_agents(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    monkeypatch.setattr("observational_memory.cli._uninstall_cron", lambda targets="both": None)
    runner = CliRunner()

    config = Config(memory_dir=tmp_path / "data" / "observational-memory", codex_home=tmp_path / "codex")
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.codex_observe_launchd_plist_path.write_text("codex")
    config.auto_memory_launchd_plist_path.write_text("auto")
    config.reflect_launchd_plist_path.write_text("reflect")
    unrelated = config.launch_agents_dir / "com.example.other.plist"
    unrelated.write_text("keep")

    subprocess_calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        subprocess_calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["uninstall", "--codex"])

    assert result.exit_code == 0, result.output
    assert not config.codex_observe_launchd_plist_path.exists()
    assert not config.auto_memory_launchd_plist_path.exists()
    assert not config.reflect_launchd_plist_path.exists()
    assert unrelated.exists()
    assert [args for args, _ in subprocess_calls] == [
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.CODEX_OBSERVE_LAUNCHD_LABEL}"],
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.AUTO_MEMORY_LAUNCHD_LABEL}"],
        ["launchctl", "bootout", f"gui/{os.getuid()}/{config.REFLECT_LAUNCHD_LABEL}"],
    ]


def test_install_launchd_only_uninstalls_targeted_cron_jobs(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "observational_memory.cli._install_launchd",
        lambda config, targets: calls.append(("launchd", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_cron",
        lambda targets="both": calls.append(("uninstall-cron", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--claude",
            "--scheduler",
            "launchd",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("launchd", "claude"), ("uninstall-cron", "claude")]


def test_install_cron_only_uninstalls_targeted_launchd_jobs(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "observational_memory.cli._install_cron",
        lambda config, targets: calls.append(("cron", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_launchd",
        lambda config, targets="both": calls.append(("uninstall-launchd", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--claude",
            "--scheduler",
            "cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("cron", "claude"), ("uninstall-launchd", "claude")]


def test_install_none_uninstalls_targeted_backstops(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "observational_memory.cli._uninstall_cron",
        lambda targets="both": calls.append(("uninstall-cron", targets)),
    )
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_launchd",
        lambda config, targets="both": calls.append(("uninstall-launchd", targets)),
    )

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--scheduler",
            "none",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("uninstall-launchd", "codex"), ("uninstall-cron", "codex")]


def test_install_claude_cron_preserves_existing_codex_cron_job(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()

    crontab_state = {
        "text": (
            "# --- observational-memory ---\n"
            "*/15 * * * * /existing/om observe --source codex 2>/dev/null\n"
            "# --- end observational-memory ---\n"
        )
    }

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    writes: list[str] = []

    def fake_run(args, **kwargs):
        if args == ["crontab", "-l"]:
            return Result(returncode=0, stdout=crontab_state["text"])
        if args == ["crontab", "-"]:
            payload = kwargs.get("input", "")
            writes.append(payload)
            crontab_state["text"] = payload
            return Result(returncode=0)
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("observational_memory.cli._install_launchd", lambda config, targets: None)
    monkeypatch.setattr("observational_memory.cli._uninstall_launchd", lambda config, targets="both": None)

    result = runner.invoke(
        cli,
        [
            "install",
            "--claude",
            "--scheduler",
            "cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert writes
    installed = writes[-1]
    assert "/existing/om observe --source codex" in installed
    assert "observe --source claude-memory" in installed
    assert "om reflect" in installed


def test_uninstall_cron_skips_write_and_message_when_no_targeted_jobs(monkeypatch, capsys):
    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["crontab", "-l"]:
            return Result(
                returncode=0,
                stdout=(
                    "# --- observational-memory ---\n"
                    "*/15 * * * * /existing/om observe --source codex 2>/dev/null\n"
                    "# --- end observational-memory ---\n"
                ),
            )
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    _uninstall_cron("claude")

    assert calls == [(["crontab", "-l"], {"capture_output": True, "text": True, "timeout": 5})]
    assert "Removed cron jobs" not in capsys.readouterr().out


def test_install_launchd_warns_without_failing_when_scheduler_setup_errors(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    monkeypatch.setattr(
        "observational_memory.cli._install_launchd",
        lambda config, targets: (_ for _ in ()).throw(click.ClickException("launchctl bootstrap failed")),
    )
    uninstall_calls = []
    monkeypatch.setattr(
        "observational_memory.cli._uninstall_cron",
        lambda targets="both": uninstall_calls.append(targets),
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--scheduler",
            "launchd",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Warning: launchd scheduler setup failed: launchctl bootstrap failed" in result.output
    assert uninstall_calls == []


def test_install_cron_warns_when_crontab_write_times_out(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli.sys.platform", "linux")
    runner = CliRunner()

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        if args == ["crontab", "-l"]:
            return Result(returncode=1, stderr="no crontab for bryan")
        if args == ["crontab", "-"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(
        cli,
        [
            "install",
            "--codex",
            "--scheduler",
            "cron",
            "--provider",
            "openai",
            "--llm-model",
            "gpt-4o-mini",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Warning: Failed to install cron jobs: timed out after 5s" in result.output


def test_uninstall_cron_warns_when_crontab_read_times_out(monkeypatch, capsys):
    def fake_run(args, **kwargs):
        if args == ["crontab", "-l"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    _uninstall_cron("codex")

    assert "Warning: Failed to read crontab: timed out after 5s" in capsys.readouterr().out


class TestGrokInstall:
    """Targeted tests for `om install --grok` (and `--all`)."""

    def test_install_grok_creates_native_hook_file(self, tmp_path, monkeypatch):
        """Basic smoke test that --grok creates the expected native hook file."""
        _set_base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        grok_home = Path(os.environ["GROK_HOME"])
        hook_file = grok_home / "hooks" / "observational-memory.json"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            ["install", "--grok", "--provider", "openai", "--llm-model", "gpt-4o-mini", "--non-interactive"],
        )

        assert result.exit_code == 0, result.output
        assert hook_file.exists()
        data = json.loads(hook_file.read_text())
        assert "hooks" in data
        # On this POSIX test machine, SessionStart should be present (no Claude OM hooks in fake env)
        assert "SessionStart" in data["hooks"]
        assert any("session-start.sh" in h["command"] for h in data["hooks"]["SessionStart"][0]["hooks"])

    def test_install_grok_omits_session_start_when_claude_om_hooks_present(self, tmp_path, monkeypatch):
        """Validates the critical anti-duplication logic for Claude compatibility layer."""
        _set_base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        home = Path(os.environ["HOME"])
        claude_dir = home / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/fake/path/to/observational-memory/hooks/claude/session-start.sh",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        grok_home = Path(os.environ["GROK_HOME"])
        hook_file = grok_home / "hooks" / "observational-memory.json"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            ["install", "--grok", "--provider", "openai", "--llm-model", "gpt-4o-mini", "--non-interactive"],
        )

        assert result.exit_code == 0, result.output
        assert hook_file.exists()
        data = json.loads(hook_file.read_text())
        # Because Claude OM SessionStart was detected, native Grok file should NOT have SessionStart
        assert "SessionStart" not in data.get("hooks", {})
        # But it should still have checkpoint events
        assert "SessionEnd" in data.get("hooks", {})
        assert "UserPromptSubmit" in data.get("hooks", {})

    def test_install_all_includes_grok(self, tmp_path, monkeypatch):
        _set_base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        grok_home = Path(os.environ["GROK_HOME"])
        hook_file = grok_home / "hooks" / "observational-memory.json"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            ["install", "--all", "--provider", "openai", "--llm-model", "gpt-4o-mini", "--non-interactive"],
        )

        assert result.exit_code == 0, result.output
        assert hook_file.exists()
