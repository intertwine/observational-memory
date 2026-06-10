"""OM Mail operations the CLI wraps: send, ask/respond negotiation, sync, accept.

Trust posture (see ``plans/email-memory-substrate.md`` §2): the provider is an
untrusted carrier, the ENVELOPE sender is authoritative for key lookup, and
every inbound trust failure — unknown sender, bad signature, undecryptable
payload, malformed envelope — quarantines the message in ``held/`` instead of
ingesting or answering it. Outbound markdown always passes the Gate-4
share-out filter, so ``scope=local`` never leaves the host over mail either.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Callable

from observational_memory.reflection_metadata import filter_reflection_document_for_shareout
from observational_memory.sync.atomic import atomic_write_text

from .account import (
    MailAccount,
    MailPeer,
    find_peer,
    hold_message,
    load_held,
    load_mail_state,
    mail_dir,
    packs_dir,
    remove_held,
    require_mail_account,
    write_mail_state,
)
from .envelope import (
    ATTACHMENT_FILENAME,
    EnvelopeError,
    MailEnvelope,
    create_envelope,
    decrypt_envelope_payload,
    envelope_subject,
    parse_envelope,
    verify_envelope,
)
from .pack import PACK_FILES, PackError, build_context_pack, open_context_pack
from .provider import MailAttachment, MailMessage, MailProvider, build_mail_provider

if TYPE_CHECKING:
    from pathlib import Path

    from observational_memory.config import Config


class MailServiceError(RuntimeError):
    """A mail operation failed a precondition or a fail-closed trust check."""


def responses_dir(config: Config) -> Path:
    return mail_dir(config) / "responses"


def _require_peer(config: Config, address: str) -> MailPeer:
    peer = find_peer(config, address)
    if peer is None:
        raise MailServiceError(f"Unknown peer: {address} (pin the peer with `om mail peers add` first).")
    return peer


def _envelope_attachment(envelope: MailEnvelope) -> tuple[MailAttachment, ...]:
    return (MailAttachment(filename=ATTACHMENT_FILENAME, content=envelope.to_bytes()),)


def _create_signed_envelope(
    account: MailAccount,
    *,
    kind: str,
    payload: dict[str, Any],
    shared_key_b64: str | None,
    request_id: str | None = None,
) -> MailEnvelope:
    return create_envelope(
        kind=kind,
        sender_address=account.address,
        sender_alias=account.display_name,
        signing_private_key_b64=account.signing_private_key_b64,
        signing_public_key_b64=account.signing_public_key_b64,
        payload=payload,
        request_id=request_id,
        shared_key_b64=shared_key_b64,
    )


def send_note(
    config: Config,
    *,
    to: str,
    markdown: str,
    subject: str | None = None,
    provider: MailProvider | None = None,
) -> dict[str, Any]:
    """Mail a memory-note to a pinned peer; encrypted when a shared key exists."""
    account = require_mail_account(config)
    peer = _require_peer(config, to)
    provider = provider or build_mail_provider(config)
    filtered = filter_reflection_document_for_shareout(markdown)
    envelope = _create_signed_envelope(
        account,
        kind="memory-note",
        payload={"subject": subject or "memory note", "markdown": filtered},
        shared_key_b64=peer.shared_key_b64,
    )
    message_id = provider.send_message(
        inbox_id=account.inbox_id,
        to=peer.address,
        subject=envelope_subject("memory-note", subject),
        text=f"OM Mail memory-note from {account.address}; machine payload in {ATTACHMENT_FILENAME}.",
        attachments=_envelope_attachment(envelope),
    )
    return {
        "message_id": message_id,
        "envelope_id": envelope.id,
        "to": peer.address,
        "encrypted": bool(peer.shared_key_b64),
    }


def send_pack(
    config: Config,
    *,
    to: str,
    include: tuple[str, ...] | None = None,
    provider: MailProvider | None = None,
) -> dict[str, Any]:
    """Mail a scope-filtered context pack. Packs are ALWAYS encrypted — no shared key, no pack."""
    account = require_mail_account(config)
    peer = _require_peer(config, to)
    if not peer.shared_key_b64:
        raise MailServiceError(f"no shared key configured for {peer.address}; packs are always encrypted")
    provider = provider or build_mail_provider(config)
    pack = build_context_pack(
        config,
        include=include or PACK_FILES,
        host_alias=account.display_name or account.address,
    )
    envelope = _create_signed_envelope(
        account,
        kind="context-pack",
        payload=pack,
        shared_key_b64=peer.shared_key_b64,
    )
    message_id = provider.send_message(
        inbox_id=account.inbox_id,
        to=peer.address,
        subject=envelope_subject("context-pack"),
        text=f"OM Mail context-pack from {account.address}; machine payload in {ATTACHMENT_FILENAME}.",
        attachments=_envelope_attachment(envelope),
    )
    return {
        "message_id": message_id,
        "envelope_id": envelope.id,
        "files": sorted(pack["files"]),
        "to": peer.address,
    }


def ask(
    config: Config,
    *,
    to: str,
    query: str,
    limit: int = 8,
    wait_seconds: float = 0.0,
    poll_interval: float = 2.0,
    provider: MailProvider | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Send a recall-request to a pinned peer; optionally poll our inbox for the answer.

    Polling matches on ``request_id == our envelope id`` and only trusts a
    response that comes from the asked peer's envelope identity, verifies
    against the pinned key, and decrypts cleanly — anything else is ignored
    here (a later ``om mail sync`` will quarantine it).
    """
    account = require_mail_account(config)
    peer = _require_peer(config, to)
    provider = provider or build_mail_provider(config)
    envelope = _create_signed_envelope(
        account,
        kind="recall-request",
        payload={"query": query, "limit": limit},
        shared_key_b64=peer.shared_key_b64,
    )
    provider.send_message(
        inbox_id=account.inbox_id,
        to=peer.address,
        subject=envelope_subject("recall-request", query[:60]),
        text=f"OM Mail recall-request from {account.address}; machine payload in {ATTACHMENT_FILENAME}.",
        attachments=_envelope_attachment(envelope),
    )
    if wait_seconds <= 0:
        return {"request_id": envelope.id, "status": "sent", "to": peer.address}

    elapsed = 0.0
    while True:
        response = _poll_for_response(config, provider, account, peer, request_id=envelope.id)
        if response is not None:
            return {"request_id": envelope.id, "status": "answered", "response": response, "to": peer.address}
        if elapsed >= wait_seconds:
            return {"request_id": envelope.id, "status": "timeout", "to": peer.address}
        _sleep(poll_interval)
        elapsed += poll_interval


def _poll_for_response(
    config: Config,
    provider: MailProvider,
    account: MailAccount,
    peer: MailPeer,
    *,
    request_id: str,
) -> dict[str, Any] | None:
    """One inbox scan for a verified, decryptable recall-response to ``request_id``."""
    for summary in provider.list_messages(inbox_id=account.inbox_id, after=None, limit=100):
        if ATTACHMENT_FILENAME not in summary.attachment_filenames:
            continue
        message = provider.get_message(inbox_id=account.inbox_id, message_id=summary.message_id)
        raw = _om_attachment_bytes(message)
        if raw is None:
            continue
        try:
            inbound = parse_envelope(raw)
        except EnvelopeError:
            continue
        if inbound.kind != "recall-response" or inbound.request_id != request_id:
            continue
        if inbound.sender_address.strip().lower() != peer.address:
            continue
        if not verify_envelope(inbound, peer.signing_public_key_b64):
            continue
        try:
            return decrypt_envelope_payload(inbound, peer.shared_key_b64)
        except EnvelopeError:
            continue
    return None


def _om_attachment_bytes(message: MailMessage) -> bytes | None:
    for attachment in message.attachments:
        if attachment.filename == ATTACHMENT_FILENAME:
            return attachment.content
    return None


def _has_shareable_body(content: str) -> bool:
    """True if any non-heading, non-comment, non-blank line survived filtering.

    Same rule as the Moss upload path: a section whose every bullet was
    ``scope=local`` strips down to its heading plus provenance stamps, and
    must be withheld entirely rather than leak the heading."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
            return True
    return False


def _run_local_recall(config: Config, query: str, limit: int) -> tuple[str, list[dict[str, Any]]]:
    """Fail-closed local recall, same path `om recall` uses: a backend that is
    not ready or whose search() raises degrades to "unavailable", never a crash.

    The local index legitimately contains ``scope=local`` content, so every
    result passes the Gate-4 share-out filter before it can leave the host —
    the same single filter the cluster and Moss paths use. A result with no
    shareable body left is dropped, not sent as a bare heading."""
    from observational_memory.search import get_backend
    from observational_memory.startup_memory import _strip_om_metadata
    from observational_memory.talk import RecallStatus

    backend = get_backend(config.search_backend, config)
    if backend.is_ready():
        try:
            results = backend.search(query, limit=limit)
            recall_status = RecallStatus.OK.value if results else RecallStatus.EMPTY.value
        except Exception:
            results = []
            recall_status = RecallStatus.UNAVAILABLE.value
    else:
        results = []
        recall_status = RecallStatus.UNAVAILABLE.value
    payloads = []
    for result in results:
        shared = filter_reflection_document_for_shareout(result.document.content)
        if not _has_shareable_body(shared):
            continue
        metadata = dict(result.document.metadata)
        payloads.append(
            {
                "rank": result.rank,
                "heading": result.document.heading,
                "content": _strip_om_metadata(shared)[:500],
                "source_path": metadata.get("file_path"),
            }
        )
    if not payloads and recall_status == RecallStatus.OK.value:
        recall_status = RecallStatus.EMPTY.value
    return recall_status, payloads


def _ingest_note(config: Config, *, sender_address: str, payload: dict[str, Any]) -> None:
    """Append an accepted memory-note as an observations block with visible
    ``source=mail:<address>`` provenance, in the house observer format."""
    from observational_memory.observe import _append_observations

    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise MailServiceError("memory-note payload has no markdown content.")
    subject = str(payload.get("subject") or "memory note")
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    block = (
        f"## {date} — Mail note from {sender_address} [source=mail:{sender_address}]\n\n"
        f'- 🟡 {hhmm} Accepted OM Mail memory-note "{subject}" from {sender_address} '
        f"(source=mail:{sender_address})\n\n"
        f"{markdown.rstrip()}\n"
    )
    _append_observations(block, config)


# Ceiling for the widening fetch when a full page is entirely already-seen.
# Past this, sync makes no progress rather than skipping (pathological inboxes
# with >1000 messages sharing one timestamp).
_MAX_FETCH_LIMIT = 1000


def _rewind_cursor(cursor: str | None) -> str | None:
    """One second before the cursor, so its timestamp bucket is re-fetched.

    Preserves the cursor's fractional-seconds style: providers like localdir
    compare timestamps as strings, so the rewound value must stay in the same
    format family. An unparseable cursor degrades to a full re-fetch (`None`),
    which seen-ids dedup makes safe, never to a skip.
    """
    if not cursor:
        return None
    try:
        parsed = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return None
    rewound = parsed - timedelta(seconds=1)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in cursor else "%Y-%m-%dT%H:%M:%SZ"
    return rewound.astimezone(timezone.utc).strftime(fmt)


def _bare_address(sender: str) -> str:
    """Lower-cased addr-spec from a sender that may carry a display name."""
    return parseaddr(sender)[1].strip().lower()


def mail_sync(
    config: Config,
    *,
    respond: bool = False,
    provider: MailProvider | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Fetch, verify, and route inbound OM Mail. Idempotent via cursor + seen ids.

    Every trust failure holds the message (never ingest, never answer);
    every processed message id is marked seen exactly once.
    """
    account = require_mail_account(config)
    provider = provider or build_mail_provider(config)
    state = load_mail_state(config)
    seen = set(state.seen_ids)

    report: dict[str, Any] = {
        "fetched": 0,
        "ingested": 0,
        "held": 0,
        "responded": 0,
        "packs": 0,
        "responses": 0,
        "skipped": 0,
        "details": [],
    }
    max_timestamp = state.cursor or ""

    # Fetch from one second BEFORE the cursor: providers filter strictly
    # after the cursor, so messages sharing the cursor's timestamp would
    # otherwise be skipped forever when a page boundary cut through their
    # tie bucket (seen-ids cannot recover messages that are never fetched).
    # Re-fetched already-processed messages are deduped by seen-ids. When a
    # full page is entirely seen, widen the fetch so a seen prefix larger
    # than `limit` cannot mask unseen ties behind it.
    after_param = _rewind_cursor(state.cursor)
    fetch_limit = max(limit, 1)
    while True:
        summaries = provider.list_messages(inbox_id=account.inbox_id, after=after_param, limit=fetch_limit)
        has_unseen = any(s.message_id not in seen for s in summaries)
        if has_unseen or len(summaries) < fetch_limit or fetch_limit >= _MAX_FETCH_LIMIT:
            break
        fetch_limit = min(fetch_limit * 2, _MAX_FETCH_LIMIT)
    report["fetched"] = len(summaries)
    processed = 0
    own_address = account.address.strip().lower()
    for summary in summaries:
        if summary.message_id in seen:
            if summary.timestamp and summary.timestamp > max_timestamp:
                max_timestamp = summary.timestamp
            report["skipped"] += 1
            continue
        if _bare_address(summary.sender) == own_address:
            # Some providers (AgentMail) list the inbox's own sent mail
            # alongside received mail. Skip it before any trust check so
            # outbound messages never resurface as held "unknown sender".
            # A spoofed transport sender lands here too — silently dropped,
            # which is as fail-closed as holding it.
            if summary.timestamp and summary.timestamp > max_timestamp:
                max_timestamp = summary.timestamp
            report["skipped"] += 1
            report["details"].append(
                {"message_id": summary.message_id, "action": "skipped", "reason": "own sent message"}
            )
            seen.add(summary.message_id)
            state.seen_ids.append(summary.message_id)
            continue
        if processed >= limit:
            # Leave the cursor behind the unprocessed tail: it is picked up
            # by the next sync instead of being skipped.
            break
        if summary.timestamp and summary.timestamp > max_timestamp:
            max_timestamp = summary.timestamp
        action, reason = _process_inbound(config, account, provider, summary.message_id, summary, respond=respond)
        report[action] += 1
        detail: dict[str, Any] = {"message_id": summary.message_id, "action": action}
        if reason:
            detail["reason"] = reason
        report["details"].append(detail)
        seen.add(summary.message_id)
        state.seen_ids.append(summary.message_id)
        processed += 1

    state.cursor = max_timestamp or None
    write_mail_state(config, state)
    return report


def _process_inbound(
    config: Config,
    account: MailAccount,
    provider: MailProvider,
    message_id: str,
    summary: Any,
    *,
    respond: bool,
) -> tuple[str, str | None]:
    """Route one inbound message; returns (report counter, optional reason)."""
    if ATTACHMENT_FILENAME not in summary.attachment_filenames:
        return "skipped", f"no {ATTACHMENT_FILENAME} attachment (plain email)"

    message = provider.get_message(inbox_id=account.inbox_id, message_id=message_id)
    raw = _om_attachment_bytes(message)
    if raw is None:
        return "skipped", f"no {ATTACHMENT_FILENAME} attachment (plain email)"

    def _hold(reason: str, *, envelope: MailEnvelope | None = None, raw_bytes: bytes | None = None) -> tuple[str, str]:
        hold_message(
            config,
            message_id=message_id,
            sender=message.sender,
            subject=message.subject,
            reason=reason,
            envelope=envelope,
            raw=raw_bytes,
        )
        return "held", reason

    try:
        envelope = parse_envelope(raw)
    except EnvelopeError as exc:
        return _hold(f"malformed envelope: {exc}", raw_bytes=raw)

    # The envelope sender is authoritative for key lookup; the transport-level
    # sender is only a fallback alias. Either way the pinned key decides.
    peer = find_peer(config, envelope.sender_address) or find_peer(config, message.sender)
    if peer is None:
        return _hold("unknown sender (pin the peer with om mail peers add)", envelope=envelope)
    if not verify_envelope(envelope, peer.signing_public_key_b64):
        return _hold("signature verification failed", envelope=envelope)
    try:
        payload = decrypt_envelope_payload(envelope, peer.shared_key_b64)
    except EnvelopeError as exc:
        return _hold(str(exc), envelope=envelope)

    if envelope.kind == "memory-note":
        if not peer.auto_accept:
            # The held record stores the (still-encrypted) envelope; accept_held
            # re-verifies and re-decrypts from it, so no plaintext copy is kept.
            return _hold("awaiting explicit accept (om mail accept)", envelope=envelope)
        try:
            _ingest_note(config, sender_address=peer.address, payload=payload)
        except MailServiceError as exc:
            return _hold(str(exc), envelope=envelope)
        return "ingested", None

    if envelope.kind == "context-pack":
        try:
            open_context_pack(payload, packs_dir(config) / envelope.id)
        except PackError as exc:
            return _hold(f"invalid context pack: {exc}", envelope=envelope)
        return "packs", None

    if envelope.kind == "recall-request":
        if not peer.allow_recall:
            return _hold("recall-request held (peer not allowed)", envelope=envelope)
        if not respond:
            return _hold("recall-request pending (run om mail sync --respond)", envelope=envelope)
        query = str(payload.get("query") or "")
        try:
            result_limit = int(payload.get("limit") or 8)
        except (TypeError, ValueError):
            result_limit = 8
        recall_status, results = _run_local_recall(config, query, max(1, min(result_limit, 50)))
        response = _create_signed_envelope(
            account,
            kind="recall-response",
            payload={"recall_status": recall_status, "results": results},
            shared_key_b64=peer.shared_key_b64,
            request_id=envelope.id,
        )
        provider.send_message(
            inbox_id=account.inbox_id,
            to=peer.address,
            subject=envelope_subject("recall-response"),
            text=f"OM Mail recall-response from {account.address}; machine payload in {ATTACHMENT_FILENAME}.",
            attachments=_envelope_attachment(response),
            in_reply_to=message_id,
        )
        return "responded", None

    if envelope.kind == "recall-response":
        request_id = envelope.request_id
        if not request_id:
            return _hold("recall-response missing request_id", envelope=envelope)
        directory = responses_dir(config)
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in request_id)[:120]
        atomic_write_text(
            directory / f"{safe_name}.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
        return "responses", None

    return _hold(f"unsupported mail kind: {envelope.kind}", envelope=envelope)


def accept_held(config: Config, message_id: str) -> dict[str, Any]:
    """Explicitly ingest a held memory-note, re-running every trust check first."""
    record = load_held(config, message_id)
    if record is None:
        raise MailServiceError(f"No held message: {message_id}")
    envelope_data = record.get("envelope")
    if not isinstance(envelope_data, dict):
        raise MailServiceError(f"Held message {message_id} has no stored envelope; reject it instead.")
    try:
        envelope = parse_envelope(json.dumps(envelope_data).encode("utf-8"))
    except EnvelopeError as exc:
        raise MailServiceError(f"Held envelope is malformed: {exc}") from exc
    if envelope.kind != "memory-note":
        raise MailServiceError(f"Held message {message_id} is a {envelope.kind}, not a memory-note.")
    peer = find_peer(config, envelope.sender_address)
    if peer is None:
        raise MailServiceError(f"Sender {envelope.sender_address} is not a pinned peer; refusing to ingest.")
    if not verify_envelope(envelope, peer.signing_public_key_b64):
        raise MailServiceError("Held envelope fails signature verification against the pinned key.")
    try:
        payload = decrypt_envelope_payload(envelope, peer.shared_key_b64)
    except EnvelopeError as exc:
        raise MailServiceError(f"Held envelope payload cannot be decrypted: {exc}") from exc
    _ingest_note(config, sender_address=peer.address, payload=payload)
    remove_held(config, message_id)
    return {
        "ingested": True,
        "sender": peer.address,
        "subject": str(payload.get("subject") or "memory note"),
    }


def reject_held(config: Config, message_id: str) -> bool:
    """Discard a held message without ingesting it."""
    return remove_held(config, message_id)
