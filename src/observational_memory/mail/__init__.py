"""OM Mail — email inboxes as a portable agent-memory substrate (experimental).

See ``plans/email-memory-substrate.md`` for the design. The mail provider is an
untrusted carrier: every envelope is signed, peers are pinned locally, context
packs are encrypted with out-of-band keys, and everything fails closed.
"""

from .provider import (
    InboxInfo,
    MailAttachment,
    MailMessage,
    MailMessageSummary,
    MailProvider,
    MailProviderError,
    build_mail_provider,
)

__all__ = [
    "InboxInfo",
    "MailAttachment",
    "MailMessage",
    "MailMessageSummary",
    "MailProvider",
    "MailProviderError",
    "build_mail_provider",
]
