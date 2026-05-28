"""Fail-closed behavior for the shell SessionStart hooks (issue #67).

The Claude, Grok, and Cowork SessionStart hooks must route all startup
context through `om context`. When `om context` fails or `om` is missing they
must fail closed: emit no agent context, write one diagnostic line to stderr,
and exit cleanly. They must never cat raw generated memory files.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "observational_memory"

HOOK_SCRIPTS = {
    "claude": PACKAGE_ROOT / "hooks" / "claude" / "session-start.sh",
    "grok": PACKAGE_ROOT / "hooks" / "grok" / "session-start.sh",
    "cowork": PACKAGE_ROOT / "cowork_plugin" / "hooks" / "scripts" / "session-start.sh",
}

# Sentinels that would only appear if a raw generated-memory fallback ran.
SECRET_PROFILE = "SECRET-PROFILE-CONTENT-SHOULD-NEVER-LEAK"
SECRET_ACTIVE = "SECRET-ACTIVE-CONTENT-SHOULD-NEVER-LEAK"
SECRET_REF = "SECRET-REFLECTIONS-CONTENT-SHOULD-NEVER-LEAK"
SECRET_OBS = "SECRET-OBSERVATIONS-CONTENT-SHOULD-NEVER-LEAK"


def _seed_memory_files(xdg_data: Path) -> None:
    """Write recognizable generated memory files the hook must never emit."""
    mem_dir = xdg_data / "observational-memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "profile.md").write_text(SECRET_PROFILE + "\n")
    (mem_dir / "active.md").write_text(SECRET_ACTIVE + "\n")
    (mem_dir / "reflections.md").write_text(SECRET_REF + "\n")
    (mem_dir / "observations.md").write_text(SECRET_OBS + "\n")


def _make_fake_om(bin_dir: Path, *, exit_code: int) -> None:
    """Create a fake `om` on PATH that exits with the given code, no output."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_om = bin_dir / "om"
    fake_om.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    fake_om.chmod(fake_om.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_hook(script: Path, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("agent", sorted(HOOK_SCRIPTS))
def test_hook_fails_closed_when_om_context_fails(agent, tmp_path):
    script = HOOK_SCRIPTS[agent]
    xdg_data = tmp_path / "data"
    _seed_memory_files(xdg_data)

    bin_dir = tmp_path / "bin"
    _make_fake_om(bin_dir, exit_code=1)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_DATA_HOME"] = str(xdg_data)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    proc = _run_hook(script, env, cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    # No agent context emitted at all.
    assert proc.stdout.strip() == ""
    # Exactly one diagnostic line on stderr.
    assert "om context unavailable - run om doctor" in proc.stderr
    # Generated memory contents must never leak into hook output.
    combined = proc.stdout + proc.stderr
    for secret in (SECRET_PROFILE, SECRET_ACTIVE, SECRET_REF, SECRET_OBS):
        assert secret not in combined


@pytest.mark.parametrize("agent", sorted(HOOK_SCRIPTS))
def test_hook_fails_closed_when_om_missing(agent, tmp_path):
    script = HOOK_SCRIPTS[agent]
    xdg_data = tmp_path / "data"
    _seed_memory_files(xdg_data)

    # Isolated bin dir with no `om`; keep coreutils available for the script.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["XDG_DATA_HOME"] = str(xdg_data)
    # Restrict PATH to a directory without `om` (plus standard tool dirs so
    # bash/command can still resolve coreutils).
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

    proc = _run_hook(script, env, cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert "om context unavailable - run om doctor" in proc.stderr
    combined = proc.stdout + proc.stderr
    for secret in (SECRET_PROFILE, SECRET_ACTIVE, SECRET_REF, SECRET_OBS):
        assert secret not in combined


@pytest.mark.parametrize("agent", sorted(HOOK_SCRIPTS))
def test_hook_passes_through_om_context_output(agent, tmp_path):
    """When `om context` succeeds, the hook emits its stdout verbatim."""
    script = HOOK_SCRIPTS[agent]
    xdg_data = tmp_path / "data"
    _seed_memory_files(xdg_data)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_om = bin_dir / "om"
    fake_om.write_text(
        "#!/usr/bin/env bash\n"
        # Echo the args so we can assert the hook calls `context --for <agent>`.
        'echo "{\\"ok\\": true, \\"args\\": \\"$*\\"}"\n'
        "exit 0\n"
    )
    fake_om.chmod(fake_om.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_DATA_HOME"] = str(xdg_data)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    proc = _run_hook(script, env, cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert '"ok": true' in proc.stdout
    assert f"context --for {agent}" in proc.stdout
    assert "om context unavailable" not in proc.stderr
    for secret in (SECRET_PROFILE, SECRET_ACTIVE, SECRET_REF, SECRET_OBS):
        assert secret not in proc.stdout
