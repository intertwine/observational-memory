"""Tests for the AgentMail REST provider against a recording fake — no network."""

import base64
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import pytest

from observational_memory.mail.provider import MailAttachment, MailProviderError
from observational_memory.mail.providers import agentmail
from observational_memory.mail.providers.agentmail import AgentMailProvider

BASE = "https://api.agentmail.to/v0"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeHTTP:
    """Recording urlopen stand-in: pops the first route matching each request."""

    def __init__(self, routes):
        # routes: list of (method, url_fragment, payload). payload is a dict
        # (JSON response), bytes (raw body), or an Exception to raise.
        self.routes = list(routes)
        self.calls = []

    def __call__(self, request, timeout=None):
        headers = {key.lower(): value for key, value in request.header_items()}
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": headers,
                "body": json.loads(request.data.decode()) if request.data else None,
                "timeout": timeout,
            }
        )
        for index, (method, fragment, payload) in enumerate(self.routes):
            if method == request.get_method() and fragment in request.full_url:
                del self.routes[index]
                if isinstance(payload, Exception):
                    raise payload
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return _FakeResponse(body)
        raise AssertionError(f"Unexpected request: {request.get_method()} {request.full_url}")


@pytest.fixture
def fake_http(monkeypatch):
    def install(routes):
        fake = _FakeHTTP(routes)
        monkeypatch.setattr(agentmail, "urlopen", fake)
        return fake

    return install


def _provider() -> AgentMailProvider:
    return AgentMailProvider(api_key="test-key", base_url=BASE)


def _http_error(code: int, reason: str, body: bytes) -> HTTPError:
    return HTTPError("https://api.agentmail.to/v0/x", code, reason, {}, io.BytesIO(body))


class TestConstruction:
    @pytest.mark.parametrize("api_key", [None, ""])
    def test_missing_api_key_raises(self, api_key):
        with pytest.raises(MailProviderError, match="OM_AGENTMAIL_API_KEY"):
            AgentMailProvider(api_key=api_key)


class TestCreateInbox:
    def test_create_inbox_maps_response(self, fake_http):
        fake = fake_http(
            [
                (
                    "POST",
                    "/v0/inboxes",
                    {
                        "pod_id": "pod_1",
                        "inbox_id": "alpha@agentmail.to",
                        "email": "alpha@agentmail.to",
                        "display_name": "Alpha",
                        "created_at": "2026-06-10T12:00:00Z",
                        "updated_at": "2026-06-10T12:00:00Z",
                    },
                )
            ]
        )
        inbox = _provider().create_inbox(username="alpha", display_name="Alpha")
        assert inbox.provider == "agentmail"
        assert inbox.inbox_id == "alpha@agentmail.to"
        assert inbox.address == "alpha@agentmail.to"
        assert inbox.display_name == "Alpha"

        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"{BASE}/inboxes"
        assert call["headers"]["authorization"] == "Bearer test-key"
        assert call["headers"]["content-type"] == "application/json"
        assert call["body"] == {"username": "alpha", "display_name": "Alpha"}

    def test_create_inbox_missing_fields_raises(self, fake_http):
        fake_http([("POST", "/v0/inboxes", {"pod_id": "pod_1"})])
        with pytest.raises(MailProviderError, match="inbox_id/email"):
            _provider().create_inbox()


class TestSendMessage:
    def test_send_message_posts_json_with_base64_attachment(self, fake_http):
        fake = fake_http(
            [("POST", "/v0/inboxes/alpha%40agentmail.to/messages/send", {"message_id": "msg_1", "thread_id": "thr_1"})]
        )
        payload = b"\x00\x01envelope bytes"
        message_id = _provider().send_message(
            inbox_id="alpha@agentmail.to",
            to="beta@agentmail.to",
            subject="[om-mail] memory-note",
            text="see attachment",
            attachments=(MailAttachment(filename="om-mail.json", content=payload, content_type="application/json"),),
            in_reply_to="msg_0",  # accepted but not sent yet
        )
        assert message_id == "msg_1"

        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"{BASE}/inboxes/{quote('alpha@agentmail.to', safe='')}/messages/send"
        assert call["headers"]["authorization"] == "Bearer test-key"
        assert call["body"] == {
            "to": ["beta@agentmail.to"],
            "subject": "[om-mail] memory-note",
            "text": "see attachment",
            "attachments": [
                {
                    "filename": "om-mail.json",
                    "content_type": "application/json",
                    "content": base64.b64encode(payload).decode("ascii"),
                }
            ],
        }
        assert "in_reply_to" not in call["body"]


class TestListMessages:
    def _summary(self, n, **extra):
        return {
            "message_id": f"msg_{n}",
            "thread_id": f"thr_{n}",
            "from": "beta@agentmail.to",
            "to": ["alpha@agentmail.to"],
            "subject": f"subject {n}",
            "timestamp": f"2026-06-10T12:00:0{n}Z",
            "preview": "...",
            "attachments": [{"attachment_id": f"att_{n}", "filename": "om-mail.json", "size": 10}],
            **extra,
        }

    def test_follows_page_tokens_and_honors_after_and_limit(self, fake_http):
        fake = fake_http(
            [
                (
                    "GET",
                    "/messages?",
                    {
                        "count": 4,
                        "limit": 3,
                        "next_page_token": "tok2",
                        "messages": [self._summary(1), self._summary(2)],
                    },
                ),
                (
                    "GET",
                    "/messages?",
                    {
                        "count": 4,
                        "limit": 3,
                        "next_page_token": "tok3",
                        "messages": [self._summary(3), self._summary(4)],
                    },
                ),
            ]
        )
        summaries = _provider().list_messages(inbox_id="alpha@agentmail.to", after="2026-06-10T11:00:00Z", limit=3)
        # Pagination stops at `limit` even though a third token was offered.
        assert [s.message_id for s in summaries] == ["msg_1", "msg_2", "msg_3"]
        assert summaries[0].sender == "beta@agentmail.to"
        assert summaries[0].subject == "subject 1"
        assert summaries[0].attachment_filenames == ("om-mail.json",)

        first_url, second_url = fake.calls[0]["url"], fake.calls[1]["url"]
        assert first_url.startswith(f"{BASE}/inboxes/{quote('alpha@agentmail.to', safe='')}/messages?")
        assert "limit=3" in first_url
        assert "after=2026-06-10T11%3A00%3A00Z" in first_url
        assert "page_token" not in first_url
        assert "page_token=tok2" in second_url
        assert "after=2026-06-10T11%3A00%3A00Z" in second_url
        # Oldest-first is load-bearing: the sync cursor advances to the max
        # processed timestamp, so newest-first pages plus a backlog larger
        # than `limit` would skip the unfetched middle forever.
        assert "ascending=true" in first_url
        assert "ascending=true" in second_url

    def test_stops_without_next_page_token(self, fake_http):
        fake = fake_http(
            [("GET", "/messages?", {"count": 1, "limit": 50, "next_page_token": None, "messages": [self._summary(1)]})]
        )
        summaries = _provider().list_messages(inbox_id="alpha@agentmail.to")
        assert len(summaries) == 1
        assert len(fake.calls) == 1


class TestGetMessage:
    def test_two_step_attachment_download(self, fake_http):
        raw_bytes = b"\x89PNG raw attachment bytes"
        presigned_url = "https://files.agentmail.example/presigned/att_1?sig=abc"
        fake = fake_http(
            [
                (
                    "GET",
                    "/messages/msg_1",
                    {
                        "message_id": "msg_1",
                        "thread_id": "thr_1",
                        "from": "beta@agentmail.to",
                        "to": ["alpha@agentmail.to"],
                        "subject": "hello",
                        "text": "body text",
                        "html": "<p>body text</p>",
                        "timestamp": "2026-06-10T12:00:00Z",
                        "in_reply_to": "msg_0",
                        "attachments": [
                            {"attachment_id": "att_1", "filename": "om-mail.json", "content_type": "application/json"}
                        ],
                    },
                ),
                (
                    "GET",
                    "/messages/msg_1/attachments/att_1",
                    {
                        "attachment_id": "att_1",
                        "download_url": presigned_url,
                        "expires_at": "2026-06-10T13:00:00Z",
                        "filename": "om-mail.json",
                        "content_type": "application/json",
                    },
                ),
                ("GET", "/presigned/att_1", raw_bytes),
            ]
        )
        message = _provider().get_message(inbox_id="alpha@agentmail.to", message_id="msg_1")
        assert message.message_id == "msg_1"
        assert message.sender == "beta@agentmail.to"
        assert message.to == ("alpha@agentmail.to",)
        assert message.text == "body text"
        assert message.in_reply_to == "msg_0"
        assert len(message.attachments) == 1
        assert message.attachments[0].filename == "om-mail.json"
        assert message.attachments[0].content_type == "application/json"
        assert message.attachments[0].content == raw_bytes

        # First two calls hit the API with auth; the presigned fetch must not.
        assert fake.calls[0]["headers"]["authorization"] == "Bearer test-key"
        assert fake.calls[1]["headers"]["authorization"] == "Bearer test-key"
        assert fake.calls[2]["url"] == presigned_url
        assert "authorization" not in fake.calls[2]["headers"]


class TestErrorMapping:
    def test_http_401_maps_to_one_liner(self, fake_http):
        fake_http([("GET", "/messages?", _http_error(401, "Unauthorized", b'{"error": "invalid api key"}'))])
        with pytest.raises(MailProviderError) as excinfo:
            _provider().list_messages(inbox_id="alpha@agentmail.to")
        message = str(excinfo.value)
        assert "HTTP 401" in message
        assert "invalid api key" in message
        assert "\n" not in message

    def test_http_500_maps_to_one_liner(self, fake_http):
        fake_http([("POST", "/v0/inboxes", _http_error(500, "Server Error", b"internal\nerror" + b"x" * 500))])
        with pytest.raises(MailProviderError) as excinfo:
            _provider().create_inbox()
        message = str(excinfo.value)
        assert "AgentMail POST /inboxes failed: HTTP 500" in message
        assert "\n" not in message
        assert len(message) < 300  # body snippet is truncated

    def test_url_error_maps_to_provider_error(self, fake_http):
        fake_http([("GET", "/messages?", URLError("connection refused"))])
        with pytest.raises(MailProviderError, match="connection refused"):
            _provider().list_messages(inbox_id="alpha@agentmail.to")
