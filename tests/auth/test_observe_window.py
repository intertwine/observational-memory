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
