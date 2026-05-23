"""Loopback HTTP server + CORS callback handler for the xAI OAuth flow.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_xai_callback_cors_origin``, ``_make_xai_callback_handler``,
``_xai_start_callback_server``, ``_xai_wait_for_callback``,
``_xai_validate_loopback_redirect_uri``).
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import AuthError

XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"


def validate_loopback_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise AuthError(
            "xAI OAuth redirect_uri must use http://127.0.0.1.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    host = parsed.hostname or ""
    if host != XAI_OAUTH_REDIRECT_HOST:
        raise AuthError(
            "xAI OAuth redirect_uri must point to 127.0.0.1.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    if not parsed.port:
        raise AuthError(
            "xAI OAuth redirect_uri must include an explicit localhost port.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    return host, parsed.port, parsed.path or "/"


def _xai_cors_origin(origin: str | None) -> str:
    allowed = {"https://accounts.x.ai", "https://auth.x.ai"}
    return origin if origin in allowed else ""


def _make_callback_handler(expected_path: str) -> tuple[type[BaseHTTPRequestHandler], dict[str, Any]]:
    result: dict[str, Any] = {"code": None, "state": None, "error": None, "error_description": None}
    result_lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def _cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            allow = _xai_cors_origin(origin)
            if allow:
                self.send_header("Access-Control-Allow-Origin", allow)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return
            params = parse_qs(parsed.query)
            incoming = {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
                "error": params.get("error", [None])[0],
                "error_description": params.get("error_description", [None])[0],
            }
            if incoming["code"] is None and incoming["error"] is None:
                self.send_response(400)
                self._cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = (
                    "<html><body>"
                    "<h1>xAI authorization not received.</h1>"
                    "<p>No authorization code in this callback. Return to the "
                    "terminal and re-run <code>om login xai-oauth</code>.</p>"
                    "</body></html>"
                )
                self.wfile.write(body.encode("utf-8"))
                return
            with result_lock:
                if not (result["code"] or result["error"]):
                    result.update(incoming)
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if incoming["error"]:
                body = "<html><body><h1>xAI authorization failed.</h1>You can close this tab.</body></html>"
            else:
                body = "<html><body><h1>xAI authorization received.</h1>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _Handler, result


def start_callback_server(
    preferred_port: int = XAI_OAUTH_REDIRECT_PORT,
) -> tuple[ThreadingHTTPServer, threading.Thread, dict[str, Any], str]:
    host = XAI_OAUTH_REDIRECT_HOST
    expected_path = XAI_OAUTH_REDIRECT_PATH
    handler_cls, result = _make_callback_handler(expected_path)

    class _ReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    ports_to_try = [preferred_port]
    if preferred_port != 0:
        ports_to_try.append(0)
    server = None
    last_error: OSError | None = None
    for port in ports_to_try:
        try:
            server = _ReuseHTTPServer((host, port), handler_cls)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise AuthError(
            f"Could not bind xAI callback server on {host}:{preferred_port}: {last_error}",
            provider="xai-oauth",
            code="xai_callback_bind_failed",
        ) from last_error
    actual_port = int(server.server_address[1])
    redirect_uri = f"http://{host}:{actual_port}{expected_path}"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        daemon=True,
    )
    thread.start()
    return server, thread, result, redirect_uri


def wait_for_callback(
    server: ThreadingHTTPServer,
    thread: threading.Thread,
    result: dict[str, Any],
    *,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(5.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.1)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        thread.join(timeout=1.0)
    raise AuthError(
        "xAI authorization timed out waiting for the local callback.",
        provider="xai-oauth",
        code="xai_callback_timeout",
    )
