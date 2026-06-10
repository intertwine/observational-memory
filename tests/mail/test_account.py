"""Tests for host-local OM Mail state: account, peers, cursor state, held mail."""

import json
import sys

import pytest

from observational_memory.config import Config
from observational_memory.mail.account import (
    MailAccount,
    MailAccountError,
    MailPeer,
    MailState,
    account_path,
    find_peer,
    held_dir,
    hold_message,
    list_held,
    load_held,
    load_mail_account,
    load_mail_peers,
    load_mail_state,
    new_mail_keypair,
    peers_path,
    remove_held,
    remove_peer,
    require_mail_account,
    state_path,
    upsert_peer,
    write_mail_account,
    write_mail_state,
)

IS_POSIX = sys.platform != "win32"


@pytest.fixture
def config(tmp_path):
    return Config(memory_dir=tmp_path / "memory", env_file=tmp_path / "env")


def _make_account():
    private_b64, public_b64 = new_mail_keypair()
    return MailAccount(
        provider="localdir",
        inbox_id="alpha@om-mail.local",
        address="alpha@om-mail.local",
        display_name="Alpha Agent",
        signing_private_key_b64=private_b64,
        signing_public_key_b64=public_b64,
        created_at="2026-06-10T12:00:00Z",
    )


class TestAccount:
    def test_write_load_round_trip(self, config):
        account = _make_account()
        write_mail_account(config, account)
        assert load_mail_account(config) == account

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX file modes only")
    def test_account_file_is_owner_only(self, config):
        write_mail_account(config, _make_account())
        assert oct(account_path(config).stat().st_mode & 0o777) == "0o600"

    def test_load_missing_returns_none(self, config):
        assert load_mail_account(config) is None

    def test_require_raises_when_missing(self, config):
        with pytest.raises(MailAccountError, match="om mail init"):
            require_mail_account(config)

    def test_require_returns_account(self, config):
        account = _make_account()
        write_mail_account(config, account)
        assert require_mail_account(config) == account


class TestPeers:
    def _peer(self, address="Beta@Example.COM"):
        _, public_b64 = new_mail_keypair()
        return MailPeer(
            address=address,
            alias="beta",
            signing_public_key_b64=public_b64,
            shared_key_b64=None,
            allow_recall=True,
            auto_accept=False,
        )

    def test_upsert_find_remove_round_trip(self, config):
        peer = self._peer()
        upsert_peer(config, peer)
        # Addresses are case-normalized on write and lookup.
        found = find_peer(config, "beta@example.com")
        assert found is not None
        assert found.address == "beta@example.com"
        assert found.alias == "beta"
        assert found.signing_public_key_b64 == peer.signing_public_key_b64
        assert found.allow_recall is True
        assert find_peer(config, "BETA@EXAMPLE.COM") == found
        assert remove_peer(config, "Beta@example.com") is True
        assert find_peer(config, "beta@example.com") is None
        assert remove_peer(config, "beta@example.com") is False

    def test_upsert_overwrites_existing(self, config):
        peer = self._peer()
        upsert_peer(config, peer)
        updated = MailPeer(
            address="beta@example.com",
            alias="beta-2",
            signing_public_key_b64=peer.signing_public_key_b64,
            auto_accept=True,
        )
        upsert_peer(config, updated)
        peers = load_mail_peers(config)
        assert len(peers) == 1
        assert peers["beta@example.com"].alias == "beta-2"
        assert peers["beta@example.com"].auto_accept is True

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX file modes only")
    def test_peers_file_is_owner_only(self, config):
        upsert_peer(config, self._peer())
        assert oct(peers_path(config).stat().st_mode & 0o777) == "0o600"


class TestState:
    def test_round_trip(self, config):
        write_mail_state(config, MailState(cursor="2026-06-10T12:00:00Z", seen_ids=["m1", "m2"]))
        state = load_mail_state(config)
        assert state.cursor == "2026-06-10T12:00:00Z"
        assert state.seen_ids == ["m1", "m2"]

    def test_missing_state_is_fresh(self, config):
        state = load_mail_state(config)
        assert state.cursor is None
        assert state.seen_ids == []

    def test_corrupt_state_is_fresh(self, config):
        state_path(config).parent.mkdir(parents=True, exist_ok=True)
        state_path(config).write_text("{not json")
        state = load_mail_state(config)
        assert state.cursor is None
        assert state.seen_ids == []

    def test_seen_ids_capped_at_2000(self, config):
        write_mail_state(config, MailState(cursor=None, seen_ids=[f"m{i}" for i in range(2100)]))
        state = load_mail_state(config)
        assert len(state.seen_ids) == 2000
        assert state.seen_ids[0] == "m100"
        assert state.seen_ids[-1] == "m2099"


class TestHeld:
    def test_hold_list_load_remove_lifecycle(self, config):
        path = hold_message(
            config,
            message_id="msg-1",
            sender="mallory@example.com",
            subject="[om-mail] memory-note",
            reason="unknown sender",
        )
        assert path.exists()
        held = list_held(config)
        assert len(held) == 1
        assert held[0]["message_id"] == "msg-1"
        assert held[0]["reason"] == "unknown sender"
        loaded = load_held(config, "msg-1")
        assert loaded is not None
        assert loaded["sender"] == "mallory@example.com"
        assert remove_held(config, "msg-1") is True
        assert load_held(config, "msg-1") is None
        assert remove_held(config, "msg-1") is False

    def test_hold_with_raw_bytes(self, config):
        hold_message(
            config,
            message_id="msg-raw",
            sender="x@example.com",
            subject="s",
            reason="malformed envelope",
            raw=b"\x00garbage",
        )
        loaded = load_held(config, "msg-raw")
        assert loaded is not None
        assert "raw_b64" in loaded

    def test_unreadable_held_file_degrades_gracefully(self, config):
        held_dir(config).mkdir(parents=True, exist_ok=True)
        (held_dir(config) / "broken.json").write_text("{not json")
        held = list_held(config)
        assert len(held) == 1
        assert held[0]["message_id"] == "broken"
        assert held[0]["reason"] == "unreadable held record"
        assert load_held(config, "broken") is None

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX file modes only")
    def test_held_record_is_owner_only(self, config):
        path = hold_message(config, message_id="m", sender="s", subject="x", reason="r")
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert json.loads(path.read_text())["reason"] == "r"
