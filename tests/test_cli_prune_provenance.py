"""Gate 3: `om prune` must never refresh section-level provenance stamps.

Prune calls only ``ensure_reflection_metadata`` (per-bullet) and intentionally
never ``ensure_section_provenance``. This locks that contract at the CLI level so
a future change that wires section stamping into prune (the corruption the spec
warns about) fails loudly.
"""

from __future__ import annotations

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
    monkeypatch.delenv("OM_CLUSTER_ENABLED", raising=False)


def test_prune_does_not_refresh_section_provenance(monkeypatch, tmp_path):
    _set_base_env(monkeypatch, tmp_path)
    memory_dir = tmp_path / "data" / "observational-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    stamped = (
        "# Reflections\n\n"
        "*Last updated: 2026-05-09 09:00 UTC*\n"
        "*Last reflected: 2026-05-09*\n\n"
        "## Core Identity\n\n"
        "<!--om-section: last_reflected=2026-05-09 derived_from_obs_window=2026-05-08..2026-05-09-->\n"
        "- **Name:** Alex <!--om: id=ome_aaa kind=identity scope=cluster node=local-->\n"
    )
    (memory_dir / "reflections.md").write_text(stamped)
    (memory_dir / "observations.md").write_text("# Observations\n")

    result = CliRunner().invoke(cli, ["prune"])
    assert result.exit_code == 0, result.output

    after = (memory_dir / "reflections.md").read_text()
    # The section stamp is byte-identical (prune never refreshes provenance).
    assert "<!--om-section: last_reflected=2026-05-09 derived_from_obs_window=2026-05-08..2026-05-09-->" in after
    assert after.count("<!--om-section:") == 1
