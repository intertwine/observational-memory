"""Plugin seam tests: entry-point mail providers and CLI plugin registration.

Out-of-tree packages (including separately licensed add-ons) attach through
two entry-point groups. The seam must be loud for a *broken* plugin, silent
for an *absent* one, and a plugin must never shadow a built-in.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from observational_memory.cli import _register_cli_plugins, cli
from observational_memory.config import Config
from observational_memory.mail.provider import (
    InboxInfo,
    MailProviderError,
    build_mail_provider,
)


class _PluginProvider:
    name = "pluginmail"

    def create_inbox(self, *, username=None, display_name=None):
        return InboxInfo(provider=self.name, inbox_id="p1", address="p@plugin.test")

    def send_message(self, *, inbox_id, to, subject, text, attachments=(), in_reply_to=None):
        return "msg"

    def list_messages(self, *, inbox_id, after=None, limit=50):
        return []

    def get_message(self, *, inbox_id, message_id):
        raise MailProviderError("empty")


class _FakeEntryPoint:
    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _patch_entry_points(monkeypatch, group_map):
    import importlib.metadata

    def fake_entry_points(*, group):
        return group_map.get(group, [])

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


def _config(tmp_path):
    return Config(memory_dir=tmp_path / "memory", env_file=tmp_path / "env")


def test_provider_plugin_resolves_by_name(monkeypatch, tmp_path):
    _patch_entry_points(
        monkeypatch,
        {
            "observational_memory.mail_providers": [
                _FakeEntryPoint("pluginmail", lambda: lambda config: _PluginProvider())
            ]
        },
    )
    provider = build_mail_provider(_config(tmp_path), "pluginmail")
    assert provider.name == "pluginmail"


def test_unknown_provider_still_fails_closed(monkeypatch, tmp_path):
    _patch_entry_points(monkeypatch, {})
    with pytest.raises(MailProviderError, match="Unknown mail provider"):
        build_mail_provider(_config(tmp_path), "pluginmail")


def test_broken_provider_plugin_is_loud(monkeypatch, tmp_path):
    def exploding_factory():
        raise ImportError("missing native dep")

    _patch_entry_points(
        monkeypatch,
        {"observational_memory.mail_providers": [_FakeEntryPoint("pluginmail", exploding_factory)]},
    )
    with pytest.raises(MailProviderError, match="failed to load"):
        build_mail_provider(_config(tmp_path), "pluginmail")


def test_provider_plugin_returning_non_provider_is_rejected(monkeypatch, tmp_path):
    _patch_entry_points(
        monkeypatch,
        {"observational_memory.mail_providers": [_FakeEntryPoint("pluginmail", lambda: lambda config: object())]},
    )
    with pytest.raises(MailProviderError, match="did not return a MailProvider"):
        build_mail_provider(_config(tmp_path), "pluginmail")


def test_builtin_names_cannot_be_shadowed_by_plugins(monkeypatch, tmp_path, mocker=None):
    # A plugin registered as "localdir" must never be consulted: built-ins
    # resolve first, so the localdir path still demands OM_MAIL_LOCALDIR.
    _patch_entry_points(
        monkeypatch,
        {
            "observational_memory.mail_providers": [
                _FakeEntryPoint("localdir", lambda: lambda config: _PluginProvider())
            ]
        },
    )
    with pytest.raises(MailProviderError, match="OM_MAIL_LOCALDIR"):
        build_mail_provider(_config(tmp_path), "localdir")


def test_cli_plugin_registers_commands(monkeypatch):
    def register(root: click.Group) -> None:
        @root.command("plugin-probe")
        def plugin_probe() -> None:
            click.echo("probe ok")

    _patch_entry_points(
        monkeypatch,
        {"observational_memory.cli_plugins": [_FakeEntryPoint("probe", lambda: register)]},
    )
    try:
        _register_cli_plugins()
        result = CliRunner().invoke(cli, ["plugin-probe"])
        assert result.exit_code == 0 and "probe ok" in result.output
    finally:
        cli.commands.pop("plugin-probe", None)


def test_cli_plugin_cannot_shadow_core_command(monkeypatch):
    original = cli.commands["status"]

    def register(root: click.Group) -> None:
        @root.command("status")
        def fake_status() -> None:  # pragma: no cover - must never run
            click.echo("shadowed")

    _patch_entry_points(
        monkeypatch,
        {"observational_memory.cli_plugins": [_FakeEntryPoint("shadow", lambda: register)]},
    )
    _register_cli_plugins()
    assert cli.commands["status"] is original


def test_broken_cli_plugin_degrades_to_warning(monkeypatch):
    def exploding_loader():
        raise RuntimeError("bad wheel")

    _patch_entry_points(
        monkeypatch,
        {"observational_memory.cli_plugins": [_FakeEntryPoint("broken", exploding_loader)]},
    )
    before = dict(cli.commands)
    _register_cli_plugins()  # must not raise
    assert cli.commands == before
