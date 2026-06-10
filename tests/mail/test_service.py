"""OM Mail service tests over an in-memory fake provider.

The fake implements the ``MailProvider`` protocol and delivers by address, so
these tests exercise the real envelope signing/encryption, peer pinning,
held-message quarantine, and routing logic without any real provider code.
"""

from __future__ import annotations

import json

import pytest

from observational_memory.config import Config
from observational_memory.mail import service
from observational_memory.mail.account import (
    MailAccount,
    MailPeer,
    list_held,
    new_mail_keypair,
    new_shared_key_b64,
    upsert_peer,
    write_mail_account,
)
from observational_memory.mail.envelope import ATTACHMENT_FILENAME, create_envelope, envelope_subject
from observational_memory.mail.provider import (
    InboxInfo,
    MailAttachment,
    MailMessage,
    MailMessageSummary,
    MailProviderError,
)
from observational_memory.mail.service import (
    MailServiceError,
    accept_held,
    ask,
    mail_sync,
    reject_held,
    send_note,
    send_pack,
)

CANNED_RESULTS = [
    {"rank": 1, "heading": "Working Mode", "content": "canned recall answer", "source_path": "reflections.md"}
]


class FakeProvider:
    """In-memory MailProvider: one message list per inbox, delivery by address."""

    name = "fake"

    def __init__(self):
        self._inboxes: dict[str, list[MailMessage]] = {}
        self._inbox_by_address: dict[str, str] = {}
        self._address_by_inbox: dict[str, str] = {}
        self._counter = 0

    def register(self, address: str) -> str:
        address = address.strip().lower()
        inbox_id = f"inbox_{len(self._inboxes)}"
        self._inboxes[inbox_id] = []
        self._inbox_by_address[address] = inbox_id
        self._address_by_inbox[inbox_id] = address
        return inbox_id

    def create_inbox(self, *, username=None, display_name=None) -> InboxInfo:
        address = f"{username or f'agent{len(self._inboxes)}'}@fake.test"
        return InboxInfo(provider=self.name, inbox_id=self.register(address), address=address)

    def send_message(self, *, inbox_id, to, subject, text, attachments=(), in_reply_to=None) -> str:
        dest = self._inbox_by_address.get(to.strip().lower())
        if dest is None:
            raise MailProviderError(f"Unknown recipient: {to}")
        self._counter += 1
        message = MailMessage(
            message_id=f"msg_{self._counter:04d}",
            thread_id=None,
            sender=self._address_by_inbox[inbox_id],
            to=(to,),
            subject=subject,
            text=text,
            timestamp=f"2026-06-10T12:00:00.{self._counter:06d}Z",
            in_reply_to=in_reply_to,
            attachments=tuple(attachments),
        )
        self._inboxes[dest].append(message)
        return message.message_id

    def list_messages(self, *, inbox_id, after=None, limit=50) -> list[MailMessageSummary]:
        summaries = []
        for message in self._inboxes.get(inbox_id, []):
            if after and message.timestamp <= after:
                continue
            summaries.append(
                MailMessageSummary(
                    message_id=message.message_id,
                    thread_id=message.thread_id,
                    sender=message.sender,
                    subject=message.subject,
                    timestamp=message.timestamp,
                    attachment_filenames=tuple(a.filename for a in message.attachments),
                )
            )
            if len(summaries) >= limit:
                break
        return summaries

    def get_message(self, *, inbox_id, message_id) -> MailMessage:
        for message in self._inboxes.get(inbox_id, []):
            if message.message_id == message_id:
                return message
        raise MailProviderError(f"No such message: {message_id}")


def _make_node(tmp_path, name: str, provider: FakeProvider) -> tuple[Config, MailAccount]:
    config = Config(
        memory_dir=tmp_path / name / "memory",
        env_file=tmp_path / name / "config" / "env",
        search_backend="none",
    )
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    address = f"{name}@fake.test"
    private_b64, public_b64 = new_mail_keypair()
    account = MailAccount(
        provider="fake",
        inbox_id=provider.register(address),
        address=address,
        display_name=name,
        signing_private_key_b64=private_b64,
        signing_public_key_b64=public_b64,
        created_at="2026-06-10T00:00:00Z",
    )
    write_mail_account(config, account)
    return config, account


def _pin(
    config: Config,
    peer_account: MailAccount,
    *,
    shared_key_b64: str | None = None,
    allow_recall: bool = False,
    auto_accept: bool = False,
) -> None:
    upsert_peer(
        config,
        MailPeer(
            address=peer_account.address,
            alias=peer_account.display_name,
            signing_public_key_b64=peer_account.signing_public_key_b64,
            shared_key_b64=shared_key_b64,
            allow_recall=allow_recall,
            auto_accept=auto_accept,
        ),
    )


@pytest.fixture
def two_nodes(tmp_path):
    provider = FakeProvider()
    config_a, account_a = _make_node(tmp_path, "alice", provider)
    config_b, account_b = _make_node(tmp_path, "bob", provider)
    return provider, config_a, account_a, config_b, account_b


def _canned_recall(monkeypatch):
    monkeypatch.setattr(service, "_run_local_recall", lambda config, query, limit: ("ok", CANNED_RESULTS))


def test_ask_respond_negotiation_end_to_end(two_nodes, monkeypatch):
    provider, config_a, account_a, config_b, account_b = two_nodes
    key = new_shared_key_b64()
    _pin(config_a, account_b, shared_key_b64=key)
    _pin(config_b, account_a, shared_key_b64=key, allow_recall=True)
    _canned_recall(monkeypatch)

    asked = ask(config_a, to=account_b.address, query="what is the working mode", provider=provider)
    assert asked["status"] == "sent"
    request_id = asked["request_id"]

    report_b = mail_sync(config_b, respond=True, provider=provider)
    assert report_b["responded"] == 1
    assert report_b["held"] == 0

    report_a = mail_sync(config_a, provider=provider)
    assert report_a["responses"] == 1

    response_path = config_a.memory_dir / "mail" / "responses" / f"{request_id}.json"
    assert response_path.exists()
    stored = json.loads(response_path.read_text())
    assert stored["recall_status"] == "ok"
    assert stored["results"] == CANNED_RESULTS


def test_ask_with_wait_answers_between_polls(two_nodes, monkeypatch):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, allow_recall=True)
    _canned_recall(monkeypatch)

    def respond_during_sleep(_seconds):
        mail_sync(config_b, respond=True, provider=provider)

    result = ask(
        config_a,
        to=account_b.address,
        query="status",
        wait_seconds=10.0,
        poll_interval=2.0,
        provider=provider,
        _sleep=respond_during_sleep,
    )
    assert result["status"] == "answered"
    assert result["response"]["results"] == CANNED_RESULTS


def test_ask_with_wait_times_out_without_answer(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    result = ask(
        config_a,
        to=account_b.address,
        query="status",
        wait_seconds=4.0,
        poll_interval=2.0,
        provider=provider,
        _sleep=lambda _seconds: None,
    )
    assert result["status"] == "timeout"


def test_memory_note_auto_accept_ingests_with_provenance(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, auto_accept=True)

    sent = send_note(
        config_a,
        to=account_b.address,
        markdown="- shared fact from alice\n- secret detail <!--om: scope=local-->\n",
        subject="handoff",
        provider=provider,
    )
    assert sent["encrypted"] is False

    report = mail_sync(config_b, provider=provider)
    assert report["ingested"] == 1
    text = config_b.observations_path.read_text()
    assert "shared fact from alice" in text
    assert f"source=mail:{account_a.address}" in text
    # scope=local was filtered out at SEND time and must not be ingested.
    assert "secret detail" not in text


def test_memory_note_without_auto_accept_held_then_accept(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, auto_accept=False)

    send_note(config_a, to=account_b.address, markdown="- pending fact\n", provider=provider)
    report = mail_sync(config_b, provider=provider)
    assert report["held"] == 1
    assert report["ingested"] == 0
    assert not config_b.observations_path.exists()

    message_id = report["details"][0]["message_id"]
    accepted = accept_held(config_b, message_id)
    assert accepted["ingested"] is True
    assert accepted["sender"] == account_a.address
    assert "pending fact" in config_b.observations_path.read_text()
    assert list_held(config_b) == []


def test_reject_held_removes_without_ingesting(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a)

    send_note(config_a, to=account_b.address, markdown="- unwanted fact\n", provider=provider)
    report = mail_sync(config_b, provider=provider)
    message_id = report["details"][0]["message_id"]

    assert reject_held(config_b, message_id) is True
    assert list_held(config_b) == []
    assert not config_b.observations_path.exists()


def test_unknown_sender_is_held_never_ingested(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    # Bob does NOT pin alice.
    send_note(config_a, to=account_b.address, markdown="- stranger fact\n", provider=provider)

    report = mail_sync(config_b, provider=provider)
    assert report["held"] == 1
    assert "unknown sender" in report["details"][0]["reason"]
    assert not config_b.observations_path.exists()
    with pytest.raises(MailServiceError):
        accept_held(config_b, report["details"][0]["message_id"])


def test_tampered_envelope_is_held(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_b, account_a, auto_accept=True)

    envelope = create_envelope(
        kind="memory-note",
        sender_address=account_a.address,
        sender_alias=None,
        signing_private_key_b64=account_a.signing_private_key_b64,
        signing_public_key_b64=account_a.signing_public_key_b64,
        payload={"subject": "x", "markdown": "- original\n"},
    )
    envelope.data["payload"]["markdown"] = "- tampered after signing\n"
    provider.send_message(
        inbox_id=account_a.inbox_id,
        to=account_b.address,
        subject=envelope_subject("memory-note"),
        text="tampered",
        attachments=(MailAttachment(filename=ATTACHMENT_FILENAME, content=envelope.to_bytes()),),
    )

    report = mail_sync(config_b, provider=provider)
    assert report["held"] == 1
    assert "signature verification failed" in report["details"][0]["reason"]
    assert not config_b.observations_path.exists()


def test_encrypted_note_without_local_shared_key_is_held(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b, shared_key_b64=new_shared_key_b64())
    _pin(config_b, account_a, auto_accept=True)  # bob lost / never had the shared key

    sent = send_note(config_a, to=account_b.address, markdown="- encrypted fact\n", provider=provider)
    assert sent["encrypted"] is True

    report = mail_sync(config_b, provider=provider)
    assert report["held"] == 1
    assert "no shared key" in report["details"][0]["reason"]
    assert not config_b.observations_path.exists()


def test_send_pack_requires_shared_key(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)  # pinned, but no shared key
    config_a.reflections_path.write_text("# Reflections\n\n## Notes\n- shareable\n")
    with pytest.raises(MailServiceError, match="packs are always encrypted"):
        send_pack(config_a, to=account_b.address, provider=provider)


def test_send_pack_round_trip_strips_scope_local(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    key = new_shared_key_b64()
    _pin(config_a, account_b, shared_key_b64=key)
    _pin(config_b, account_a, shared_key_b64=key)

    config_a.profile_path.write_text("# Profile\n\n## Core Identity\n- Name: Alice\n")
    config_a.reflections_path.write_text(
        "# Reflections\n\n## Preferences & Opinions\n"
        "- shareable preference <!--om: scope=cluster-->\n"
        "- PRIVATE-PACK-SECRET <!--om: scope=local-->\n"
    )

    sent = send_pack(config_a, to=account_b.address, provider=provider)
    assert sorted(sent["files"]) == ["profile.md", "reflections.md"]

    report = mail_sync(config_b, provider=provider)
    assert report["packs"] == 1

    pack_dir = config_b.memory_dir / "mail" / "packs" / sent["envelope_id"]
    reflections = (pack_dir / "reflections.md").read_text()
    assert "shareable preference" in reflections
    assert "PRIVATE-PACK-SECRET" not in reflections
    assert "Name: Alice" in (pack_dir / "profile.md").read_text()


def test_recall_request_from_disallowed_peer_is_held_not_answered(two_nodes, monkeypatch):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, allow_recall=False)
    _canned_recall(monkeypatch)

    ask(config_a, to=account_b.address, query="secrets please", provider=provider)
    report = mail_sync(config_b, respond=True, provider=provider)
    assert report["held"] == 1
    assert report["responded"] == 0
    assert "peer not allowed" in report["details"][0]["reason"]
    # No response ever reached alice.
    assert provider.list_messages(inbox_id=account_a.inbox_id) == []


def test_mail_sync_is_idempotent(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, auto_accept=True)

    send_note(config_a, to=account_b.address, markdown="- once only\n", provider=provider)
    first = mail_sync(config_b, provider=provider)
    assert first["ingested"] == 1

    second = mail_sync(config_b, provider=provider)
    # The cursor's own timestamp bucket is deliberately re-fetched (tie-bucket
    # drain, PR #88 review); idempotency means nothing is PROCESSED twice.
    assert second["fetched"] == second["skipped"]
    assert second["ingested"] == 0
    assert second["held"] == 0
    assert second["details"] == []
    assert config_b.observations_path.read_text().count("once only") == 1


def test_plain_human_email_is_skipped_not_held(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    provider.send_message(
        inbox_id=account_a.inbox_id,
        to=account_b.address,
        subject="hello from a human",
        text="no machine payload here",
    )
    report = mail_sync(config_b, provider=provider)
    assert report["skipped"] == 1
    assert report["held"] == 0
    assert list_held(config_b) == []


def test_send_note_to_unpinned_peer_fails_closed(two_nodes):
    provider, config_a, account_a, config_b, account_b = two_nodes
    with pytest.raises(MailServiceError, match="Unknown peer"):
        send_note(config_a, to=account_b.address, markdown="- nope\n", provider=provider)


def test_rewind_cursor_preserves_format_and_fails_open(two_nodes):
    assert service._rewind_cursor(None) is None
    assert service._rewind_cursor("2026-06-10T12:00:00Z") == "2026-06-10T11:59:59Z"
    assert service._rewind_cursor("2026-06-10T12:00:00.500000Z") == "2026-06-10T11:59:59.500000Z"
    # Unparseable cursor degrades to a full re-fetch (seen-ids dedup), never a skip.
    assert service._rewind_cursor("not-a-timestamp") is None


def test_sync_drains_timestamp_ties_larger_than_limit(two_nodes):
    """PR #88 review (P1) repro: three notes share one timestamp, limit=2.

    The cursor advances to the shared timestamp after the first page; a
    strictly-after fetch would exclude the third note forever. The rewound
    cursor plus the widening fetch must drain the tie bucket instead.
    """
    from dataclasses import replace as dc_replace

    provider, config_a, account_a, config_b, account_b = two_nodes
    _pin(config_a, account_b)
    _pin(config_b, account_a, auto_accept=True)
    for index in range(3):
        send_note(
            config_a,
            to=account_b.address,
            markdown=f"- tied note {index}",
            subject=f"tied-{index}",
            provider=provider,
        )
    inbox_b = account_b.inbox_id
    provider._inboxes[inbox_b] = [
        dc_replace(message, timestamp="2026-06-10T12:00:00.000000Z") for message in provider._inboxes[inbox_b]
    ]

    first = mail_sync(config_b, provider=provider, limit=2)
    assert first["ingested"] == 2
    second = mail_sync(config_b, provider=provider, limit=2)
    assert second["ingested"] == 1
    observations = (config_b.memory_dir / "observations.md").read_text()
    for index in range(3):
        assert f"tied note {index}" in observations
    # Steady state stays idempotent: re-fetched tie-bucket messages are
    # deduped by seen-ids, nothing is reprocessed.
    third = mail_sync(config_b, provider=provider, limit=2)
    assert third["ingested"] == 0 and third["held"] == 0 and third["responded"] == 0
