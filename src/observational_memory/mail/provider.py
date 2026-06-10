"""Mail provider seam: mailbox is a role, not a vendor.

Implementations live in ``mail/providers/``. AgentMail is the first real
provider; ``localdir`` backs tests and shared-folder demos. A generic
IMAP/SMTP provider can slot in later without touching callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from observational_memory.config import Config

KNOWN_MAIL_PROVIDERS = ("agentmail", "localdir")


class MailProviderError(RuntimeError):
    """A provider operation failed (network, auth, malformed response)."""


@dataclass(frozen=True)
class InboxInfo:
    provider: str
    inbox_id: str
    address: str
    display_name: str | None = None


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/json"


@dataclass(frozen=True)
class MailMessageSummary:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    timestamp: str
    attachment_filenames: tuple[str, ...] = ()


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    thread_id: str | None
    sender: str
    to: tuple[str, ...]
    subject: str
    text: str
    timestamp: str
    in_reply_to: str | None = None
    attachments: tuple[MailAttachment, ...] = field(default_factory=tuple)


@runtime_checkable
class MailProvider(Protocol):
    """Minimal mailbox surface OM Mail needs from any email provider."""

    name: str

    def create_inbox(self, *, username: str | None = None, display_name: str | None = None) -> InboxInfo: ...

    def send_message(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        text: str,
        attachments: tuple[MailAttachment, ...] = (),
        in_reply_to: str | None = None,
    ) -> str: ...

    def list_messages(
        self, *, inbox_id: str, after: str | None = None, limit: int = 50
    ) -> list[MailMessageSummary]: ...

    def get_message(self, *, inbox_id: str, message_id: str) -> MailMessage: ...


def build_mail_provider(config: Config, provider_name: str | None = None) -> MailProvider:
    """Instantiate the configured provider; fail closed on unknown names."""
    name = (provider_name or config.mail_provider).strip().lower()
    if name == "agentmail":
        from .providers.agentmail import AgentMailProvider

        return AgentMailProvider(
            api_key=config.agentmail_api_key,
            base_url=config.agentmail_base_url,
        )
    if name == "localdir":
        from .providers.localdir import LocalDirProvider

        if not config.mail_localdir:
            raise MailProviderError("OM_MAIL_LOCALDIR must point at a directory for the localdir provider.")
        return LocalDirProvider(config.mail_localdir)
    raise MailProviderError(f"Unknown mail provider: {name!r} (known: {', '.join(KNOWN_MAIL_PROVIDERS)})")
