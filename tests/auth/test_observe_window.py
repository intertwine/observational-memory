"""Tests for the observe input-window cap (cost bound)."""

from __future__ import annotations

from observational_memory.config import Config
from observational_memory.observe import _recent_observations_window


def test_small_observations_unchanged() -> None:
    cfg = Config(observer_context_max_chars=12000)
    obs = "## 123\n\nsmall content\n"
    assert _recent_observations_window(obs, cfg) == obs


def test_large_observations_truncated_to_tail() -> None:
    cfg = Config(observer_context_max_chars=500)
    body = "\n".join(f"### Entry {i}\nsome observation line {i}" for i in range(200))
    out = _recent_observations_window(body, cfg)
    assert len(out) < len(body)
    assert "Older observations elided" in out
    # Keeps the most recent entries, drops the earliest.
    assert "Entry 199" in out
    assert "Entry 0\n" not in out


def test_cap_zero_disables() -> None:
    cfg = Config(observer_context_max_chars=0)
    body = "x" * 50000
    assert _recent_observations_window(body, cfg) == body


def test_tail_starts_on_boundary() -> None:
    cfg = Config(observer_context_max_chars=80)
    body = "### A\n" + ("alpha " * 30) + "\n### B\nbeta tail content here\n"
    out = _recent_observations_window(body, cfg)
    # Should not begin mid-word; should resume at a section/blank boundary.
    assert "Older observations elided" in out
    assert out.rstrip().endswith("beta tail content here")


def test_non_cluster_observe_sends_full_file(tmp_path, monkeypatch) -> None:
    """Regression: non-cluster observe must NOT truncate input (overwrite would lose history)."""
    import observational_memory.observe as obs
    from observational_memory.config import Config

    monkeypatch.setenv("OM_CLUSTER_ENABLED", "0")
    mem = tmp_path / "mem"
    mem.mkdir()
    cfg = Config(memory_dir=mem, observer_context_max_chars=100)
    # Big existing observations file (way over the cap).
    big = "# Observations\n\n" + "\n".join(f"## 2026-05-{d:02d}\n\nold entry {d}" for d in range(1, 28))
    cfg.observations_path.write_text(big)

    captured = {}

    def fake_compress(system_prompt, user_content, config, operation=None, **k):
        captured["user_content"] = user_content
        return "## 2026-05-23\n\nnew observation"

    monkeypatch.setattr(obs, "compress", fake_compress)
    monkeypatch.setattr(obs, "_cluster_enabled", lambda c: False)

    msgs = [
        obs.Message(role="user", content=f"m{i}", timestamp="2026-05-23T00:00:00Z", source="claude") for i in range(10)
    ]
    obs.run_observer(msgs, cfg, dry_run=True)
    # Full history present in the prompt despite a tiny cap (cap is cluster-only).
    assert "old entry 1" in captured["user_content"]
    assert "Older observations elided" not in captured["user_content"]


def test_cluster_observe_applies_cap(tmp_path, monkeypatch) -> None:
    """In cluster mode the cap applies (append-only log preserves history)."""
    import observational_memory.observe as obs
    from observational_memory.config import Config

    mem = tmp_path / "mem"
    mem.mkdir()
    cfg = Config(memory_dir=mem, observer_context_max_chars=100)
    big = "# Observations\n\n" + "\n".join(f"## 2026-05-{d:02d}\n\nold entry {d}" for d in range(1, 28))
    cfg.observations_path.write_text(big)

    captured = {}

    def fake_compress(system_prompt, user_content, config, operation=None, **k):
        captured["user_content"] = user_content
        return "new"

    monkeypatch.setattr(obs, "compress", fake_compress)
    monkeypatch.setattr(obs, "_cluster_enabled", lambda c: True)

    msgs = [
        obs.Message(role="user", content=f"m{i}", timestamp="2026-05-23T00:00:00Z", source="claude") for i in range(10)
    ]
    obs.run_observer(msgs, cfg, dry_run=True)
    assert "Older observations elided" in captured["user_content"]
    assert "old entry 1\n" not in captured["user_content"]
