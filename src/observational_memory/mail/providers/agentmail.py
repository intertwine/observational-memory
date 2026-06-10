"""AgentMail REST client: API-first, dynamically minted inboxes for agents.

Stdlib urllib only (same house style as ``sync/transports/relay.py``). Auth is
a Bearer token from ``OM_AGENTMAIL_API_KEY``; all request/response bodies are
JSON. Attachment download is a two-step dance: the API returns a short-lived
presigned ``download_url``, which is then fetched WITHOUT the API key (the
authorization lives in the URL itself).

Every transport or protocol failure is mapped to a one-line
:class:`MailProviderError`; raw tracebacks never escape this module.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..provider import (
    InboxInfo,
    MailAttachment,
    MailMessage,
    MailMessageSummary,
    MailProviderError,
)

_ERROR_BODY_SNIPPET_CHARS = 200


class AgentMailProvider:
    name = "agentmail"

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.agentmail.to/v0",
        *,
        timeout_seconds: float = 15.0,
    ):
        if not api_key:
            raise MailProviderError(
                "OM_AGENTMAIL_API_KEY is not set; export an AgentMail API key or use OM_MAIL_PROVIDER=localdir."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def create_inbox(self, *, username: str | None = None, display_name: str | None = None) -> InboxInfo:
        body: dict[str, Any] = {}
        if username:
            body["username"] = username
        if display_name:
            body["display_name"] = display_name
        data = self._request_json("POST", "/inboxes", body=body)
        inbox_id = data.get("inbox_id")
        address = data.get("email")
        if not inbox_id or not address:
            raise MailProviderError("AgentMail create-inbox response is missing inbox_id/email.")
        return InboxInfo(
            provider=self.name,
            inbox_id=str(inbox_id),
            address=str(address),
            display_name=data.get("display_name"),
        )

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
        del in_reply_to  # threading via the provider reply endpoint comes later
        body: dict[str, Any] = {
            "to": [to],
            "subject": subject,
            "text": text,
            "attachments": [
                {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "content": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in attachments
            ],
        }
        data = self._request_json("POST", f"/inboxes/{_q(inbox_id)}/messages/send", body=body)
        message_id = data.get("message_id")
        if not message_id:
            raise MailProviderError("AgentMail send response is missing message_id.")
        return str(message_id)

    def list_messages(self, *, inbox_id: str, after: str | None = None, limit: int = 50) -> list[MailMessageSummary]:
        summaries: list[MailMessageSummary] = []
        page_token: str | None = None
        while len(summaries) < limit:
            params: dict[str, Any] = {"limit": limit}
            if after:
                params["after"] = after
            if page_token:
                params["page_token"] = page_token
            data = self._request_json("GET", f"/inboxes/{_q(inbox_id)}/messages?{urlencode(params)}")
            for raw in data.get("messages") or []:
                if not isinstance(raw, dict):
                    continue
                summaries.append(_summary_from(raw))
                if len(summaries) >= limit:
                    break
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return summaries

    def get_message(self, *, inbox_id: str, message_id: str) -> MailMessage:
        data = self._request_json("GET", f"/inboxes/{_q(inbox_id)}/messages/{_q(message_id)}")
        attachments: list[MailAttachment] = []
        for meta in data.get("attachments") or []:
            if not isinstance(meta, dict) or not meta.get("attachment_id"):
                continue
            attachment_id = str(meta["attachment_id"])
            attachments.append(
                MailAttachment(
                    filename=str(meta.get("filename") or attachment_id),
                    content=self._download_attachment(inbox_id, message_id, attachment_id),
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

    def _download_attachment(self, inbox_id: str, message_id: str, attachment_id: str) -> bytes:
        # Step 1: ask the API for a short-lived presigned download URL.
        meta = self._request_json(
            "GET",
            f"/inboxes/{_q(inbox_id)}/messages/{_q(message_id)}/attachments/{_q(attachment_id)}",
        )
        download_url = meta.get("download_url")
        if not download_url:
            raise MailProviderError(f"AgentMail attachment {attachment_id} response is missing download_url.")
        # Step 2: fetch the presigned URL with NO auth header — the grant is in
        # the URL, and the API key must never be sent to the storage host.
        request = Request(str(download_url), method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read()
        except HTTPError as exc:
            raise MailProviderError(
                f"AgentMail attachment download failed: HTTP {exc.code} {_body_snippet(exc)}".rstrip()
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MailProviderError(f"AgentMail attachment download failed: {getattr(exc, 'reason', exc)}") from exc

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as exc:
            raise MailProviderError(
                f"AgentMail {method} {path} failed: HTTP {exc.code} {_body_snippet(exc)}".rstrip()
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MailProviderError(f"AgentMail {method} {path} failed: {getattr(exc, 'reason', exc)}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailProviderError(f"AgentMail {method} {path} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MailProviderError(f"AgentMail {method} {path} returned a non-object response.")
        return parsed


def _summary_from(raw: dict[str, Any]) -> MailMessageSummary:
    attachments = raw.get("attachments") or []
    return MailMessageSummary(
        message_id=str(raw.get("message_id", "")),
        thread_id=raw.get("thread_id"),
        sender=str(raw.get("from", "")),
        subject=str(raw.get("subject", "")),
        timestamp=str(raw.get("timestamp", "")),
        attachment_filenames=tuple(str(meta.get("filename", "")) for meta in attachments if isinstance(meta, dict)),
    )


def _body_snippet(error: HTTPError) -> str:
    """First ~200 chars of the error body, flattened to one line."""
    try:
        text = error.read()[:_ERROR_BODY_SNIPPET_CHARS].decode("utf-8", errors="replace")
    except Exception:
        return ""
    return " ".join(text.split())


def _q(segment: str) -> str:
    return quote(segment, safe="")
