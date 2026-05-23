"""Auth flows for om: subscription OAuth (ChatGPT / xAI) + API key passthrough.

Public surface (stable):

* ``AuthError``, ``format_auth_error``
* ``resolve_runtime_credentials(provider_id, *, force_refresh=False)``
* ``auth_status``, ``logout``, ``auth_refresh``, ``login_openai_chatgpt``,
  ``login_xai_oauth``, ``login_import``, ``login_api_key``
* ``redact_token``, ``auth_file_path``
"""

from __future__ import annotations

from .commands import (
    auth_refresh,
    auth_status,
    interactive_picker,
    login_api_key,
    login_import,
    login_openai_chatgpt,
    login_xai_oauth,
    logout,
    provider_summary_lines,
)
from .errors import AuthError, format_auth_error
from .runtime import resolve_runtime_credentials
from .store import auth_file_path, redact_token

__all__ = [
    "AuthError",
    "auth_file_path",
    "auth_refresh",
    "auth_status",
    "format_auth_error",
    "interactive_picker",
    "login_api_key",
    "login_import",
    "login_openai_chatgpt",
    "login_xai_oauth",
    "logout",
    "provider_summary_lines",
    "redact_token",
    "resolve_runtime_credentials",
]
