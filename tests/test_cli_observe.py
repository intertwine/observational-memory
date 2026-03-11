"""Tests for observe CLI reflector catch-up behavior and cron cleanup."""

from click.testing import CliRunner

from observational_memory.cli import _strip_om_cron_entries, cli


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


def test_observe_runs_reflector_catchup_after_scan(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    calls = {"count": 0}

    monkeypatch.setattr("observational_memory.observe.observe_all_codex", lambda config, dry_run: [])
    monkeypatch.setattr("observational_memory.observe.observe_all_claude", lambda config, dry_run: [])

    def fake_catchup(config):
        calls["count"] += 1

    monkeypatch.setattr("observational_memory.cli._maybe_run_reflector_catchup", fake_catchup)

    result = runner.invoke(cli, ["observe", "--source", "codex"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 1


def test_observe_skips_reflector_catchup_in_dry_run(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    runner = CliRunner()
    calls = {"count": 0}

    monkeypatch.setattr("observational_memory.observe.observe_all_codex", lambda config, dry_run: [])

    def fake_catchup(config):
        calls["count"] += 1

    monkeypatch.setattr("observational_memory.cli._maybe_run_reflector_catchup", fake_catchup)

    result = runner.invoke(cli, ["observe", "--source", "codex", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls["count"] == 0


def test_strip_om_cron_entries_removes_blocks_and_legacy_lines():
    lines = [
        "MAILTO=user@example.com",
        "# --- observational-memory ---",
        "*/15 * * * * /old/om observe --source codex",
        "# --- end observational-memory ---",
        "# --- observational-memory ---",
        "# --- end observational-memory ---",
        "0 4 * * * /old/om reflect",
        "5 * * * * /usr/bin/true",
    ]

    assert _strip_om_cron_entries(lines) == [
        "MAILTO=user@example.com",
        "5 * * * * /usr/bin/true",
    ]


def test_strip_om_cron_entries_preserves_unclosed_block_with_warning(capsys):
    lines = [
        "MAILTO=user@example.com",
        "# --- observational-memory ---",
        "*/15 * * * * /old/om observe --source codex",
        "15 9 * * * /usr/bin/true",
    ]

    assert _strip_om_cron_entries(lines) == [
        "MAILTO=user@example.com",
        "# --- observational-memory ---",
        "*/15 * * * * /old/om observe --source codex",
        "15 9 * * * /usr/bin/true",
    ]
    assert "unclosed observational-memory cron block detected" in capsys.readouterr().err
