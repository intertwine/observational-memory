"""Directory-backed mail provider for tests and shared-folder demos.

One JSON file per message under a shared root:

```text
{root}/inboxes/{address}/inbox.json                 # inbox metadata
{root}/inboxes/{address}/messages/{message_id}.json # one file per message
```

``send_message`` writes straight into the RECIPIENT's ``messages/`` directory
(creating it if absent, so mail can queue before the recipient runs init).
Writes are atomic with unique filenames, so concurrent senders are safe;
malformed message files are skipped during listing and fail closed on direct
reads. Also works machine-to-machine over any shared folder.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observational_memory.sync.atomic import atomic_write_text

from ..provider import (
    InboxInfo,
    MailAttachment,
    MailMessage,
    MailMessageSummary,
    MailProviderError,
)

_DOMAIN = "om-mail.local"


class LocalDirProvider:
    name = "localdir"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def create_inbox(self, *, username: str | None = None, display_name: str | None = None) -> InboxInfo:
        user = username or f"agent-{secrets.token_hex(3)}"
        address = f"{user}@{_DOMAIN}"
        inbox_file = self._inbox_dir(address) / "inbox.json"
        if inbox_file.exists():
            # Idempotent: re-init returns the existing inbox untouched.
            try:
                data = json.loads(inbox_file.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            return InboxInfo(
                provider=self.name,
                inbox_id=str(data.get("inbox_id") or address),
                address=str(data.get("address") or address),
                display_name=data.get("display_name"),
            )
        self._messages_dir(address).mkdir(parents=True, exist_ok=True)
        record = {
            "provider": self.name,
            "inbox_id": address,
            "address": address,
            "display_name": display_name,
            "created_at": _utc_timestamp(),
        }
        atomic_write_text(inbox_file, json.dumps(record, indent=2, sort_keys=True) + "\n")
        return InboxInfo(provider=self.name, inbox_id=address, address=address, display_name=display_name)

    def send_message(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        text: str,
        attachments: tuple[MailAttachment, ...] = (),
        in_reply_to: str | None = None,
    ) -> str:
        message_id = f"lm_{time.time_ns():x}_{secrets.token_hex(2)}"
        record: dict[str, Any] = {
            "message_id": message_id,
            "thread_id": in_reply_to or message_id,
            # For this provider the inbox id IS the sender's address.
            "from": inbox_id,
            "to": [to],
            "subject": subject,
            "text": text,
            "timestamp": _utc_timestamp(),
            "in_reply_to": in_reply_to,
            "attachments": [
                {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "content_b64": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in attachments
            ],
        }
        # Deliver into the recipient's directory, creating it if absent so
        # messages can queue before the recipient ever runs create_inbox.
        messages_dir = self._messages_dir(to)
        messages_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(messages_dir / f"{message_id}.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
        return message_id

    def list_messages(self, *, inbox_id: str, after: str | None = None, limit: int = 50) -> list[MailMessageSummary]:
        messages_dir = self._messages_dir(inbox_id)
        if not messages_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in messages_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue  # malformed or mid-write file: skip, never crash the poll
            if isinstance(data, dict) and data.get("message_id"):
                records.append(data)
        records.sort(key=lambda r: (str(r.get("timestamp", "")), str(r.get("message_id", ""))))
        if after:
            # Strictly-after string compare on timestamps. Messages that share
            # the cursor timestamp are deduped by the caller's seen-ids.
            records = [r for r in records if str(r.get("timestamp", "")) > after]
        summaries: list[MailMessageSummary] = []
        for record in records[: max(limit, 0)]:
            attachments = record.get("attachments") or []
            summaries.append(
                MailMessageSummary(
                    message_id=str(record["message_id"]),
                    thread_id=record.get("thread_id"),
                    sender=str(record.get("from", "")),
                    subject=str(record.get("subject", "")),
                    timestamp=str(record.get("timestamp", "")),
                    attachment_filenames=tuple(
                        str(meta.get("filename", "")) for meta in attachments if isinstance(meta, dict)
                    ),
                )
            )
        return summaries

    def get_message(self, *, inbox_id: str, message_id: str) -> MailMessage:
        path = self._messages_dir(inbox_id) / f"{_safe_message_id(message_id)}.json"
        if not path.exists():
            raise MailProviderError(f"localdir message not found: {message_id}")
        try:
            data = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailProviderError(f"localdir message {message_id} is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise MailProviderError(f"localdir message {message_id} is malformed.")
        attachments: list[MailAttachment] = []
        for meta in data.get("attachments") or []:
            if not isinstance(meta, dict):
                raise MailProviderError(f"localdir message {message_id} has a malformed attachment.")
            try:
                content = base64.b64decode(str(meta.get("content_b64", "")), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MailProviderError(f"localdir message {message_id} has an undecodable attachment: {exc}") from exc
            attachments.append(
                MailAttachment(
                    filename=str(meta.get("filename", "")),
                    content=content,
                    content_type=str(meta.get("content_type") or "application/octet-stream"),
                )
            )
        to = data.get("to") or []
        if isinstance(to, str):
            to = [to]
        return MailMessage(
            message_id=str(data.get("message_id") or message_id),
            thread_id=data.get("thread_id"),
            sender=str(data.get("from", "")),
            to=tuple(str(item) for item in to),
            subject=str(data.get("subject", "")),
            text=str(data.get("text", "")),
            timestamp=str(data.get("timestamp", "")),
            in_reply_to=data.get("in_reply_to"),
            attachments=tuple(attachments),
        )

    def _inbox_dir(self, address: str) -> Path:
        return self.root / "inboxes" / address

    def _messages_dir(self, address: str) -> Path:
        return self._inbox_dir(address) / "messages"


def _utc_timestamp() -> str:
    """UTC ISO with microseconds: lexically sortable and monotonic-friendly."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_message_id(message_id: str) -> str:
    """Provider-minted ids are ``lm_<hex>``; reject path-shaped ids anyway."""
    if not message_id or "/" in message_id or "\\" in message_id or message_id in {".", ".."}:
        raise MailProviderError(f"Invalid localdir message id: {message_id!r}")
    return message_id
