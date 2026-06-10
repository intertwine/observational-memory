"""Tests for the directory-backed localdir mail provider."""

import json
import re
import time

import pytest

from observational_memory.mail.provider import MailAttachment, MailProviderError
from observational_memory.mail.providers.localdir import LocalDirProvider


@pytest.fixture
def provider(tmp_path):
    return LocalDirProvider(tmp_path / "mailroot")


class TestCreateInbox:
    def test_create_inbox(self, provider):
        inbox = provider.create_inbox(username="alpha", display_name="Alpha")
        assert inbox.provider == "localdir"
        assert inbox.address == "alpha@om-mail.local"
        assert inbox.inbox_id == inbox.address
        assert inbox.display_name == "Alpha"

    def test_create_inbox_is_idempotent(self, provider):
        first = provider.create_inbox(username="alpha", display_name="Alpha")
        again = provider.create_inbox(username="alpha", display_name="Renamed")
        # Existing inbox.json wins; the second call does not rewrite it.
        assert again == first

    def test_create_inbox_generates_username(self, provider):
        inbox = provider.create_inbox()
        assert re.fullmatch(r"agent-[0-9a-f]{6}@om-mail\.local", inbox.address)


class TestSendAndReceive:
    def test_send_list_get_round_trip_with_attachment(self, provider):
        sender = provider.create_inbox(username="alpha")
        recipient = provider.create_inbox(username="beta")
        payload = b"\x00\x01\xffraw envelope bytes"
        message_id = provider.send_message(
            inbox_id=sender.inbox_id,
            to=recipient.address,
            subject="[om-mail] memory-note",
            text="see attachment",
            attachments=(MailAttachment(filename="om-mail.json", content=payload),),
        )
        summaries = provider.list_messages(inbox_id=recipient.inbox_id)
        assert [s.message_id for s in summaries] == [message_id]
        assert summaries[0].sender == sender.address
        assert summaries[0].subject == "[om-mail] memory-note"
        assert summaries[0].attachment_filenames == ("om-mail.json",)

        message = provider.get_message(inbox_id=recipient.inbox_id, message_id=message_id)
        assert message.message_id == message_id
        assert message.sender == sender.address
        assert message.to == (recipient.address,)
        assert message.text == "see attachment"
        assert message.in_reply_to is None
        assert len(message.attachments) == 1
        assert message.attachments[0].filename == "om-mail.json"
        assert message.attachments[0].content == payload

    def test_in_reply_to_sets_thread(self, provider):
        sender = provider.create_inbox(username="alpha")
        recipient = provider.create_inbox(username="beta")
        first = provider.send_message(inbox_id=sender.inbox_id, to=recipient.address, subject="q", text="?")
        reply = provider.send_message(
            inbox_id=recipient.inbox_id,
            to=sender.address,
            subject="re: q",
            text="!",
            in_reply_to=first,
        )
        message = provider.get_message(inbox_id=sender.inbox_id, message_id=reply)
        assert message.in_reply_to == first
        assert message.thread_id == first

    def test_two_inboxes_are_isolated(self, provider):
        alpha = provider.create_inbox(username="alpha")
        beta = provider.create_inbox(username="beta")
        provider.send_message(inbox_id=alpha.inbox_id, to=beta.address, subject="s", text="t")
        assert provider.list_messages(inbox_id=alpha.inbox_id) == []
        assert len(provider.list_messages(inbox_id=beta.inbox_id)) == 1

    def test_queue_before_recipient_init(self, provider):
        # Mail can be delivered before the recipient ever runs create_inbox.
        sender = provider.create_inbox(username="alpha")
        message_id = provider.send_message(
            inbox_id=sender.inbox_id, to="late@om-mail.local", subject="early", text="queued"
        )
        late = provider.create_inbox(username="late")
        summaries = provider.list_messages(inbox_id=late.inbox_id)
        assert [s.message_id for s in summaries] == [message_id]


class TestListing:
    def _send_n(self, provider, sender, recipient, count):
        message_ids = []
        for i in range(count):
            message_ids.append(
                provider.send_message(inbox_id=sender.inbox_id, to=recipient.address, subject=f"m{i}", text=str(i))
            )
            time.sleep(0.002)  # distinct microsecond timestamps
        return message_ids

    def test_after_cursor_filters_strictly_after(self, provider):
        sender = provider.create_inbox(username="alpha")
        recipient = provider.create_inbox(username="beta")
        message_ids = self._send_n(provider, sender, recipient, 3)
        all_summaries = provider.list_messages(inbox_id=recipient.inbox_id)
        assert [s.message_id for s in all_summaries] == message_ids
        cursor = all_summaries[0].timestamp
        later = provider.list_messages(inbox_id=recipient.inbox_id, after=cursor)
        assert [s.message_id for s in later] == message_ids[1:]
        # The cursor of the newest message yields nothing further.
        assert provider.list_messages(inbox_id=recipient.inbox_id, after=all_summaries[-1].timestamp) == []

    def test_limit(self, provider):
        sender = provider.create_inbox(username="alpha")
        recipient = provider.create_inbox(username="beta")
        message_ids = self._send_n(provider, sender, recipient, 3)
        summaries = provider.list_messages(inbox_id=recipient.inbox_id, limit=2)
        assert [s.message_id for s in summaries] == message_ids[:2]

    def test_malformed_message_file_is_skipped(self, provider, tmp_path):
        sender = provider.create_inbox(username="alpha")
        recipient = provider.create_inbox(username="beta")
        message_id = provider.send_message(inbox_id=sender.inbox_id, to=recipient.address, subject="ok", text="t")
        messages_dir = tmp_path / "mailroot" / "inboxes" / recipient.address / "messages"
        (messages_dir / "broken.json").write_text("{definitely not json")
        (messages_dir / "wrong-shape.json").write_text(json.dumps(["a", "list"]))
        summaries = provider.list_messages(inbox_id=recipient.inbox_id)
        assert [s.message_id for s in summaries] == [message_id]


class TestGetMessageFailures:
    def test_missing_message_raises(self, provider):
        inbox = provider.create_inbox(username="alpha")
        with pytest.raises(MailProviderError, match="not found"):
            provider.get_message(inbox_id=inbox.inbox_id, message_id="lm_missing")

    def test_malformed_message_raises(self, provider, tmp_path):
        inbox = provider.create_inbox(username="alpha")
        messages_dir = tmp_path / "mailroot" / "inboxes" / inbox.address / "messages"
        (messages_dir / "lm_bad.json").write_text("{not json")
        with pytest.raises(MailProviderError, match="unreadable"):
            provider.get_message(inbox_id=inbox.inbox_id, message_id="lm_bad")

    def test_path_shaped_message_id_rejected(self, provider):
        inbox = provider.create_inbox(username="alpha")
        with pytest.raises(MailProviderError, match="Invalid"):
            provider.get_message(inbox_id=inbox.inbox_id, message_id="../../inbox")
