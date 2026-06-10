"""Tests for om doctor enterprise provider checks."""

import json
import os
import subprocess

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.search.qmd import QMDIndexInfo, QMDInstallInfo


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
        "OM_SEARCH_BACKEND",
        "OM_QMD_INDEX_NAME",
        "OM_QMD_NO_RERANK",
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
        "<!-- observational-memory:codex-hooks-fallback-v2 -->\n"
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


def test_doctor_codex_hooks_feature_accepts_canonical_flag(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    codex_home = tmp_path / "codex"
    (codex_home / "config.toml").write_text("[features]\nhooks = true\n")

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    feature_check = _get_check(data, "Codex hooks feature")
    assert feature_check is not None
    assert feature_check["status"] == "PASS"


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
        "<!-- observational-memory:codex-hooks-fallback-v2 -->\n"
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


def test_doctor_flags_invalid_cowork_hooks_json(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    plugin_dir = (
        tmp_path
        / "home"
        / "Library"
        / "Application Support"
        / "Claude"
        / "local-agent-mode-plugins"
        / "observational-memory"
    )
    (plugin_dir / "hooks").mkdir(parents=True)
    (plugin_dir / "hooks" / "hooks.json").write_text('{"SessionStart": []}\n')

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    check = _get_check(data, "Cowork hooks.json")
    assert check is not None
    assert check["status"] == "FAIL"
    assert check["detail"] == "missing top-level hooks object"


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


def test_status_warns_when_crontab_permission_denied(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setattr("observational_memory.cli.sys.platform", "darwin")
    runner = CliRunner()

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "print"]:
            return Result(returncode=1, stderr="not loaded")
        if args == ["crontab", "-l"]:
            raise PermissionError(1, "Operation not permitted", "crontab")
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "Cron jobs: error ([Errno 1] Operation not permitted: 'crontab')" in result.output


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


def test_doctor_reports_qmd_hybrid_health(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OM_SEARCH_BACKEND", "qmd-hybrid")
    monkeypatch.setenv("OM_QMD_INDEX_NAME", "om-review")
    monkeypatch.setenv("OM_QMD_NO_RERANK", "1")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_install",
        lambda env_overrides=None: QMDInstallInfo(
            available=True,
            binary_path="/tmp/bin/qmd",
            supports_index=True,
            supports_no_rerank=True,
            supports_bench=True,
            help_output="--index\n--no-rerank\nqmd bench",
        ),
    )
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_index",
        lambda index_name, collection_name, env_overrides=None: QMDIndexInfo(
            index_name=index_name,
            collection_name=collection_name,
            collection_exists=True,
            index_path="/tmp/om-review.sqlite",
            total_files=21,
            vectors_embedded=21,
            pending_vectors=0,
            updated="1m ago",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert _get_check(data, "QMD binary")["status"] == "PASS"
    assert _get_check(data, "QMD 2.1 features")["status"] == "PASS"
    assert _get_check(data, "QMD collection")["status"] == "PASS"
    assert _get_check(data, "QMD rerank mode")["status"] == "PASS"
    assert _get_check(data, "QMD embeddings")["status"] == "PASS"


def test_doctor_warns_when_qmd_hybrid_has_no_embeddings(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OM_SEARCH_BACKEND", "qmd-hybrid")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_install",
        lambda env_overrides=None: QMDInstallInfo(
            available=True,
            binary_path="/tmp/bin/qmd",
            supports_index=True,
            supports_no_rerank=False,
            supports_bench=False,
            help_output="--index",
        ),
    )
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_index",
        lambda index_name, collection_name, env_overrides=None: QMDIndexInfo(
            index_name=index_name,
            collection_name=collection_name,
            collection_exists=True,
            total_files=21,
            vectors_embedded=0,
            pending_vectors=21,
            updated="1m ago",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert _get_check(data, "QMD 2.1 features")["status"] == "WARN"
    assert _get_check(data, "QMD embeddings")["status"] == "WARN"
    assert "0 embedded vectors" in _get_check(data, "QMD embeddings")["detail"]


def test_status_reports_qmd_backend_details(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_SEARCH_BACKEND", "qmd-hybrid")
    monkeypatch.setenv("OM_QMD_INDEX_NAME", "om-review")
    monkeypatch.setenv("OM_QMD_NO_RERANK", "1")
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_install",
        lambda env_overrides=None: QMDInstallInfo(
            available=True,
            binary_path="/tmp/bin/qmd",
            supports_index=True,
            supports_no_rerank=True,
            supports_bench=True,
            help_output="--index\n--no-rerank\nqmd bench",
        ),
    )
    monkeypatch.setattr(
        "observational_memory.search.qmd.inspect_qmd_index",
        lambda index_name, collection_name, env_overrides=None: QMDIndexInfo(
            index_name=index_name,
            collection_name=collection_name,
            collection_exists=True,
            index_path="/tmp/om-review.sqlite",
            total_files=21,
            vectors_embedded=10,
            pending_vectors=11,
            updated="3m ago",
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "Search:" in result.output
    assert "Backend: qmd-hybrid" in result.output
    assert "QMD binary: /tmp/bin/qmd" in result.output
    assert "QMD index: om-review" in result.output
    assert "Hybrid rerank: disabled via OM_QMD_NO_RERANK=1" in result.output
    assert "Collection: observational-memory" in result.output
    assert "Embedded vectors: 10" in result.output
    assert "Pending vectors: 11" in result.output


def test_doctor_includes_memory_growth_block(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(
        "# Reflections\n\n"
        "## Core Identity\n"
        "<!--om-section: last_reflected=2026-06-01 derived_from_obs_window=2026-05-20..2026-05-30-->\n"
        "- Name: Bryan\n\n"
        "## Active Projects\n### Alpha\n- Status: Active\n"
    )

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    growth = _get_check(data, "Memory growth (B0)")
    assert growth is not None
    assert growth["status"] == "PASS"
    assert "reflections.md" in growth["detail"]
    assert "2 section(s)" in growth["detail"]

    largest = _get_check(data, "Memory growth: largest section")
    assert largest is not None
    assert largest["status"] == "PASS"

    coldest = _get_check(data, "Memory growth: coldest section")
    assert coldest is not None
    assert coldest["status"] == "PASS"
    assert "Core Identity" in coldest["detail"]


def test_doctor_memory_growth_with_no_memory_files(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    growth = _get_check(data, "Memory growth (B0)")
    assert growth is not None
    assert growth["status"] == "PASS"
    assert "reflections.md not found" in growth["detail"]


def test_doctor_never_fails_when_growth_measurement_raises(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("observational_memory.cli._import_provider_sdk", lambda provider: None)

    def _boom(config, **kwargs):
        raise RuntimeError("growth blew up")

    monkeypatch.setattr("observational_memory.growth.measure_memory_growth", _boom)
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    growth = _get_check(data, "Memory growth (B0)")
    assert growth is not None
    assert growth["status"] == "WARN"
    assert "growth blew up" in growth["detail"]
