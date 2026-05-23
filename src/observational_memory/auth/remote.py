"""Remote-session detection + SSH-tunnel / manual-paste hints.

Ported from upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23, functions
``_is_remote_session``, ``_print_loopback_ssh_hint``,
``_parse_pasted_callback``, ``_prompt_manual_callback_paste``).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import parse_qs, urlparse


def is_remote_session() -> bool:
    """True when loopback OAuth cannot reach the user's local browser."""
    if os.getenv("SSH_CLIENT") or os.getenv("SSH_TTY"):
        return True
    for var in (
        "CLOUD_SHELL",
        "CODESPACES",
        "CODESPACE_NAME",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "STACKBLITZ",
    ):
        if os.getenv(var):
            return True
    return False


def _ssh_user_at_host() -> str:
    try:
        hostname = socket.gethostname() or "<this-host>"
    except OSError:
        hostname = "<this-host>"
    user = os.getenv("USER") or os.getenv("LOGNAME") or "<user>"
    return f"{user}@{hostname}"


def print_loopback_ssh_hint(redirect_uri: str, *, docs_url: str | None = None) -> None:
    """Print an SSH-tunnel hint when running a loopback flow over SSH/cloud shell."""
    if not is_remote_session():
        return
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return
    host = parsed.hostname or ""
    port = parsed.port
    if host not in {"127.0.0.1", "::1", "localhost"} or not port:
        return
    divider = "-" * 60
    print()
    print(divider)
    print("Remote session detected — SSH tunnel required")
    print(divider)
    print(f"om is waiting for the OAuth callback on {redirect_uri}")
    print("but your browser is on a different machine. Run this command")
    print("in a NEW terminal on your local machine BEFORE opening the URL:")
    print()
    print(f"  ssh -N -L {port}:127.0.0.1:{port} {_ssh_user_at_host()}")
    print()
    print("Then open the authorize URL above in your local browser.")
    print()
    print(
        "No SSH client (Cloud Shell / Codespaces / web IDE)?  Re-run with "
        "`--manual-paste` to skip the loopback listener and paste the failed "
        "callback URL directly."
    )
    if docs_url:
        print(f"Provider docs: {docs_url}")
    print(divider)
    print()


def parse_pasted_callback(raw: str) -> dict:
    """Parse a pasted callback URL / query string into the loopback dict shape."""
    stripped = (raw or "").strip()
    result: dict = {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }
    if not stripped:
        return result
    query = ""
    if stripped.startswith(("http://", "https://")):
        try:
            parsed = urlparse(stripped)
        except Exception:
            return result
        query = parsed.query or ""
    elif stripped.startswith("?"):
        query = stripped[1:]
    elif "=" in stripped:
        query = stripped
    else:
        result["code"] = stripped
        return result
    params = parse_qs(query, keep_blank_values=False)
    for key in ("code", "state", "error", "error_description"):
        values = params.get(key)
        if values:
            result[key] = values[0]
    return result


def prompt_manual_callback_paste(redirect_uri: str) -> dict:
    """Read a callback URL from stdin (manual-paste fallback)."""
    print()
    print("--- Manual callback paste ---------------------------------------")
    print("After approving in your browser, your browser will try to load")
    print(f"  {redirect_uri}")
    print("which fails (the loopback listener is on this remote machine,")
    print("not on your laptop) — that is expected.  Copy the FULL URL")
    print("from your browser's address bar of that failed page and paste")
    print("it below.  A bare '?code=...&state=...' fragment also works.")
    print("-----------------------------------------------------------------")
    try:
        raw = input("Callback URL: ")
    except (EOFError, KeyboardInterrupt):
        raw = ""
    return parse_pasted_callback(raw)
