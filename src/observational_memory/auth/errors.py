"""Structured auth errors with UX mapping hints.

Mirrors the shape of upstream Hermes (nousresearch/hermes-agent
hermes_cli/auth.py blob 5fd3676b, 2026-05-23) so the same error
codes flow through and the upstream incident docs remain useful.
"""

from __future__ import annotations


class AuthError(RuntimeError):
    """An auth flow failure with a structured code and relogin hint."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: str | None = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


def format_auth_error(error: Exception) -> str:
    """Map an AuthError to concise user-facing guidance."""
    if not isinstance(error, AuthError):
        return str(error)
    if error.code == "xai_oauth_tier_denied":
        return (
            f"{error}\n\n"
            "Hint: switch to the metered xAI path by setting XAI_API_KEY and "
            "OM_LLM_PROVIDER=xai, or upgrade at https://x.ai/grok."
        )
    if error.relogin_required:
        provider = error.provider or "the provider"
        return f"{error} Run `om login {provider}` to re-authenticate."
    return str(error)
