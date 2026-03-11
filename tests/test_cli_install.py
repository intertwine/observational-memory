"""Tests for om install provider onboarding."""

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


def test_install_generates_compact_files_and_updates_codex_block(monkeypatch, tmp_path):
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

    updated_agents = codex_agents.read_text()
    assert "profile.md" in updated_agents
    assert "active.md" in updated_agents
    assert "om search" in updated_agents
