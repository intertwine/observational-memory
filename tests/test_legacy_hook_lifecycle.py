"""Legacy teardown must preserve other tools and leave recovery evidence."""

import json
from types import SimpleNamespace

import click
import pytest

from observational_memory.cli import (
    _hook_command_exists,
    _install_claude_hooks,
    _is_om_claude_hook,
    _uninstall_claude_hooks,
)


def test_mixed_hooks_roundtrip_preserves_unrelated_entries(tmp_path):
    path = tmp_path / "settings.json"
    unrelated = {"type": "command", "command": "echo keep", "timeout": 8}
    old = {"type": "command", "command": "'/old path/bin/om' claude-checkpoint"}
    original = {
        "model": "unchanged",
        "hooks": {
            "SessionEnd": [{"matcher": "*", "extra": 9, "hooks": [old, unrelated]}, {"malformed": "retain"}],
            "SessionStart": [{"hooks": [{"type": "prompt", "prompt": "Keep this"}]}],
            "PostToolUse": [{"hooks": [unrelated]}],
        },
    }
    path.write_text(json.dumps(original))
    config = SimpleNamespace(claude_settings_path=path)
    _install_claude_hooks(config)
    first = path.read_bytes()
    _install_claude_hooks(config)
    assert path.read_bytes() == first
    _uninstall_claude_hooks(config)
    result = json.loads(path.read_text())
    original["hooks"]["SessionEnd"][0]["hooks"] = [unrelated]
    assert result == original
    first = path.read_bytes()
    _uninstall_claude_hooks(config)
    assert path.read_bytes() == first
    backups = list(tmp_path.glob("settings.json.om-backup-*"))
    assert len(backups) == 2
    assert all(p.stat().st_mode & 0o077 == 0 for p in backups)


@pytest.mark.parametrize("content", ["[]", '{"hooks": []}', '{"hooks": {"SessionStart": {}}}', "not json"])
@pytest.mark.parametrize("operation", [_install_claude_hooks, _uninstall_claude_hooks])
def test_malformed_settings_fail_without_writing(tmp_path, content, operation):
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(click.ClickException):
        operation(SimpleNamespace(claude_settings_path=path))
    assert path.read_text() == content
    assert not list(tmp_path.glob("*.om-backup-*"))


@pytest.mark.parametrize(
    "command",
    [
        "om context",
        "'/a path/om' claude-checkpoint",
        "/bin/om.exe context",
        "/old/lib/observational_memory/hooks/claude/session-start.sh",
        "'/old path/lib/observational_memory/hooks/claude/session-start.sh'",
        "bash '/old path/lib/observational_memory/hooks/claude/session-end.sh'",
        "/old path/lib/observational_memory/hooks/claude/session-start.sh",
    ],
)
def test_owned_legacy_commands(command):
    assert _is_om_claude_hook(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo om context",
        "echo /lib/observational_memory/hooks/claude/session-start.sh",
        "om context && echo keep",
        "/bin/other context",
        "'unterminated",
        None,
        123,
        "/bin/echo hi; /lib/observational_memory/hooks/claude/session-start.sh",
    ],
)
def test_unrelated_commands_not_owned(command):
    assert not _is_om_claude_hook(command)


def test_executable_with_spaces_and_arguments(tmp_path):
    executable = tmp_path / "an executable"
    executable.write_text("unused")
    executable.chmod(0o700)
    assert _hook_command_exists(f"'{executable}' context")
    assert not _hook_command_exists(f"'{tmp_path}' context")
    assert not _hook_command_exists(f"'{tmp_path / 'missing'}' context")
    assert not _hook_command_exists("'unterminated")


def test_symlinked_settings_not_modified(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    path = tmp_path / "settings.json"
    path.symlink_to(target)
    with pytest.raises(click.ClickException, match="symlinked"):
        _install_claude_hooks(SimpleNamespace(claude_settings_path=path))
    assert target.read_text() == "{}"
