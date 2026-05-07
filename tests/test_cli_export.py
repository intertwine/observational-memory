"""Tests for the om export command."""

from click.testing import CliRunner

from observational_memory.cli import cli


def _set_base_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    codex_home = tmp_path / "codex"
    for path in (home, xdg_config, xdg_data, codex_home):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_export_command_writes_target_bundle(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reflections.md").write_text(
        "# Reflections\n\n## Core Identity\n- **Name:** Bryan\n\n## Active Projects\n- OM export work\n"
    )
    (memory_dir / "observations.md").write_text(
        "# Observations\n\n## 2026-05-07\n\n### Current Context\n- **Active task:** Export command\n"
    )

    output_dir = tmp_path / "bundle"
    result = CliRunner().invoke(cli, ["export", "--target", "chatgpt", "--output", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert "Exported chatgpt memory bundle" in result.output
    assert (output_dir / "chatgpt-memory-seed.md").exists()
    assert (output_dir / "manifest.json").exists()
