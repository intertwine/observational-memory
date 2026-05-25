"""Implementations of ``om login``, ``om logout``, ``om auth ...``."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import click

from . import openai_chatgpt as _chatgpt
from . import xai_oauth as _xai
from .cli_import import detect_cli_imports, import_provider
from .errors import AuthError, format_auth_error
from .runtime import resolve_runtime_credentials
from .store import (
    auth_store_lock,
    delete_provider_state,
    load_auth_store,
    redact_token,
    save_auth_store,
    save_provider_state,
)

_TOS_FLAG_KEY = "tos_acknowledged_at"
_TOS_NOTICE = (
    "Notice: by logging in, you confirm that om's use of your subscription "
    "complies with the provider's terms of service."
)

_PROVIDER_LABELS = {
    "openai-chatgpt": "OpenAI ChatGPT subscription",
    "xai-oauth": "xAI Grok (SuperGrok subscription)",
    "openai": "OpenAI (API key)",
    "anthropic": "Anthropic (API key)",
    "xai": "xAI Grok (API key)",
}


def _maybe_print_tos(store: dict) -> None:
    meta = store.setdefault("meta", {})
    if meta.get(_TOS_FLAG_KEY):
        return
    click.echo(_TOS_NOTICE)
    meta[_TOS_FLAG_KEY] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _persist_provider(provider_id: str, state: dict, *, set_active: bool = True) -> None:
    with auth_store_lock():
        store = load_auth_store()
        _maybe_print_tos(store)
        save_provider_state(store, provider_id, state, set_active=set_active)
        save_auth_store(store)


def _read_env_vars(path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _model_matches_provider(provider_id: str, model: str) -> bool:
    normalized = model.strip().lower()
    if not normalized:
        return True
    if provider_id == "openai-chatgpt":
        return normalized.startswith(("gpt-", "o1", "o3", "o4", "chatgpt", "codex-"))
    if provider_id == "xai-oauth":
        return normalized.startswith("grok-")
    return True


def _subscription_default_model(provider_id: str, cfg) -> str | None:
    if provider_id == "openai-chatgpt":
        return cfg.openai_chatgpt_model
    if provider_id == "xai-oauth":
        return cfg.xai_oauth_model
    return None


def _model_reconciliation_updates(provider_id: str, cfg) -> dict[str, str]:
    """Return model env updates needed after making a subscription provider default.

    `om login` is often used to switch away from a previously configured API-key
    provider. A stale global model such as a Claude model under
    `OM_LLM_PROVIDER=openai-chatgpt` fails at request time. Update incompatible
    model overrides while preserving compatible user choices.
    """
    default_model = _subscription_default_model(provider_id, cfg)
    if provider_id not in {"openai-chatgpt", "xai-oauth"} or not default_model:
        return {}

    env_values = _read_env_vars(cfg.env_file)

    def current(name: str) -> str:
        return (os.environ.get(name) or env_values.get(name) or "").strip()

    updates: dict[str, str] = {}
    for name, provider_override_name in (
        ("OM_LLM_MODEL", None),
        ("OM_LLM_OBSERVER_MODEL", "OM_LLM_OBSERVER_PROVIDER"),
        ("OM_LLM_REFLECTOR_MODEL", "OM_LLM_REFLECTOR_PROVIDER"),
    ):
        value = current(name)
        if not value:
            continue
        override = current(provider_override_name) if provider_override_name else ""
        if override and override.lower() not in {"auto", provider_id}:
            continue
        if not _model_matches_provider(provider_id, value):
            updates[name] = default_model
    return updates


def _set_default_provider(provider_id: str) -> None:
    """Write OM_LLM_PROVIDER to the env file so `auto` doesn't keep an API key.

    Without this, a user who has ANTHROPIC_API_KEY / OPENAI_API_KEY set logs in
    successfully but `auto` resolution keeps using the metered key (rules 1-2
    win over subscriptions). Pinning the provider after an explicit login is
    what makes the subscription actually take effect.
    """
    from ..config import Config

    cfg = Config()
    env_values = _read_env_vars(cfg.env_file)
    previous = (os.environ.get("OM_LLM_PROVIDER") or env_values.get("OM_LLM_PROVIDER") or "").strip()
    model_updates = _model_reconciliation_updates(provider_id, cfg)
    _append_env_var("OM_LLM_PROVIDER", provider_id)
    os.environ["OM_LLM_PROVIDER"] = provider_id
    for name, value in model_updates.items():
        _append_env_var(name, value)
        os.environ[name] = value
    cfg = Config()
    if previous and previous.lower() not in {"auto", provider_id}:
        click.echo(f"Set OM_LLM_PROVIDER={provider_id} in {cfg.env_file} (was {previous}).")
    else:
        click.echo(f"Set OM_LLM_PROVIDER={provider_id} in {cfg.env_file}.")
    for name, value in model_updates.items():
        click.echo(f"Set {name}={value} in {cfg.env_file} for {provider_id}.")


def login_openai_chatgpt(*, open_browser: bool = True, set_default: bool = True) -> dict:
    """Run device-code login for ChatGPT and persist tokens."""
    state = _chatgpt.device_code_login(open_browser=open_browser)
    _persist_provider("openai-chatgpt", state)
    email = (state.get("id_token_claims") or {}).get("email") or "ChatGPT subscriber"
    click.echo(f"\nSigned in as {email}.")
    click.echo("Wrote tokens to ~/.config/observational-memory/auth.json")
    if set_default:
        _set_default_provider("openai-chatgpt")
    click.echo("Next: try `om observe` — om will use your ChatGPT subscription.")
    return state


def login_xai_oauth(
    *,
    open_browser: bool = True,
    manual_paste: bool = False,
    timeout_seconds: float | None = None,
    set_default: bool = True,
) -> dict:
    """Run the xAI loopback PKCE login and persist tokens."""
    state = _xai.loopback_login(
        open_browser=open_browser,
        manual_paste=manual_paste,
        timeout_seconds=timeout_seconds,
    )
    _persist_provider("xai-oauth", state)
    click.echo("\nSigned in to xAI Grok (SuperGrok).")
    click.echo("Wrote tokens to ~/.config/observational-memory/auth.json")
    if set_default:
        _set_default_provider("xai-oauth")
    click.echo("Next: try `om observe` — om will use your SuperGrok subscription.")
    return state


def login_import(*, provider: str | None = None) -> list[str]:
    """Import sibling-CLI tokens (~/.codex/auth.json, ~/.grok/auth.json).

    If ``provider`` is None, import everything detected.
    """
    detected = detect_cli_imports()
    targets: list[str] = []
    if provider:
        if provider not in detected:
            raise AuthError(
                f"No sibling-CLI tokens detected for {provider!r}.",
                provider=provider,
                code="cli_import_missing",
            )
        targets = [provider]
    else:
        targets = list(detected.keys())
    if not targets:
        click.echo("Nothing to import: no ~/.codex/auth.json or ~/.grok/auth.json found.")
        return []
    imported: list[str] = []
    for pid in targets:
        state = import_provider(pid)
        _persist_provider(pid, state, set_active=False)
        imported.append(pid)
        click.echo(f"Imported {pid} from {state.get('source')}.")
    return imported


def login_api_key(target: str, key: str | None = None) -> None:
    """Persist an API key into the env file (no auth.json entry)."""
    target = target.strip().lower()
    env_var = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "xai": "XAI_API_KEY",
    }.get(target)
    if not env_var:
        raise AuthError(
            f"Unknown API-key target {target!r}. Use openai, anthropic, or xai.",
            provider=target,
        )
    if key is None or not key.strip():
        key = click.prompt(f"Paste your {target} API key", hide_input=True).strip()
    _append_env_var(env_var, key)
    click.echo(f"Wrote {env_var} to the env file.")


def _append_env_var(name: str, value: str) -> None:
    from ..config import Config

    cfg = Config()
    cfg.ensure_env_file()
    text = cfg.env_file.read_text(encoding="utf-8") if cfg.env_file.exists() else ""
    lines = text.splitlines()
    kept: list[str] = []
    found = False
    for line in lines:
        if line.startswith(f"{name}="):
            kept.append(f"{name}={value}")
            found = True
        else:
            kept.append(line)
    if not found:
        kept.append(f"{name}={value}")
    cfg.env_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        cfg.env_file.chmod(0o600)
    except OSError:
        pass


def logout(provider: str | None = None) -> list[str]:
    """Remove provider tokens from the auth store."""
    removed: list[str] = []
    with auth_store_lock():
        store = load_auth_store()
        providers = list((store.get("providers") or {}).keys())
        targets = [provider] if provider else providers
        for pid in targets:
            if delete_provider_state(store, pid):
                removed.append(pid)
        save_auth_store(store)
    return removed


def provider_summary_lines(config=None) -> list[str]:
    """Shared LLM/provider summary used by both `om status` and `om auth status`.

    Reports the provider/model that will actually run each workflow, the stored
    subscription tokens (redacted), and warns when subscription tokens exist but
    `auto` resolution will keep using a metered API key instead.
    """
    from ..config import Config

    cfg = config or Config()
    lines: list[str] = []

    # Resolved default provider (may raise if nothing is configured).
    resolved: str | None = None
    try:
        resolved = cfg.resolve_provider()
        lines.append(f"  Resolved provider: {resolved}")
    except RuntimeError as exc:
        lines.append(f"  Resolved provider: (unresolved) — {exc}")

    # Per-workflow provider + model (mirrors llm.compress resolution).
    def _op(operation: str) -> str:
        from ..llm import _infer_provider

        op_provider = cfg.operation_provider(operation)
        if op_provider:
            model = cfg.resolve_model(operation, op_provider, ignore_global_model=True)
            return f"{op_provider} / {model}"
        base = resolved
        if base is None:
            return "(unresolved)"
        model = cfg.resolve_model(operation, base)
        eff = _infer_provider(model, base)
        return f"{eff} / {model}"

    lines.append(f"  Observer:  {_op('observer')}")
    lines.append(f"  Reflector: {_op('reflector')}")

    # Stored subscription tokens (redacted).
    store = load_auth_store(cfg)
    sub_providers = store.get("providers") or {}
    if sub_providers:
        lines.append("  Subscription tokens:")
        for pid, state in sub_providers.items():
            if not isinstance(state, dict):
                continue
            tokens = state.get("tokens") or {}
            tail = redact_token(tokens.get("access_token"))
            exp = state.get("expires_at") or "?"
            lines.append(f"    - {pid}: {tail} (expires {exp})")
    else:
        lines.append("  Subscription tokens: none (run `om login`)")

    # Footgun: tokens present but a metered key wins under `auto`.
    if (cfg.llm_provider or "auto").strip().lower() == "auto":
        for sub in ("openai-chatgpt", "xai-oauth"):
            if sub in sub_providers and resolved not in (sub, None) and resolved in ("anthropic", "openai", "xai"):
                lines.append(
                    f"  ⚠ {sub} tokens are stored but provider resolves to '{resolved}' "
                    f"(API key wins under auto). Run `om login {sub}` or set OM_LLM_PROVIDER={sub}."
                )
                break
    return lines


def auth_status(*, as_json: bool = False) -> dict:
    """Return a redacted snapshot of stored providers."""
    store = load_auth_store()
    providers_meta = []
    for pid, state in (store.get("providers") or {}).items():
        if not isinstance(state, dict):
            continue
        tokens = state.get("tokens") or {}
        providers_meta.append(
            {
                "provider": pid,
                "label": _PROVIDER_LABELS.get(pid, pid),
                "auth_mode": state.get("auth_mode"),
                "expires_at": state.get("expires_at"),
                "last_refresh": state.get("last_refresh"),
                "source": state.get("source"),
                "base_url": state.get("base_url"),
                "access_token": redact_token(tokens.get("access_token")),
                "has_refresh_token": bool(tokens.get("refresh_token")),
            }
        )
    snapshot = {
        "active_provider": store.get("active_provider"),
        "providers": providers_meta,
        "api_keys_present": {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "XAI_API_KEY": bool(os.environ.get("XAI_API_KEY")),
        },
    }
    if as_json:
        click.echo(json.dumps(snapshot, indent=2, sort_keys=True))
        return snapshot
    if not providers_meta and not any(snapshot["api_keys_present"].values()):
        click.echo("No providers configured. Run `om login` to get started.")
        return snapshot
    click.echo("LLM:")
    for line in provider_summary_lines():
        click.echo(line)
    click.echo()
    click.echo(f"Stored auth (active_provider in auth.json): {snapshot['active_provider'] or '(none)'}\n")
    if providers_meta:
        click.echo("Subscription providers (from auth.json):")
        for p in providers_meta:
            click.echo(f"  - {p['provider']:<16} {p['label']}")
            click.echo(f"      access_token: {p['access_token']}")
            click.echo(f"      expires_at:   {p['expires_at'] or '(unknown)'}")
            click.echo(f"      last_refresh: {p['last_refresh'] or '(unknown)'}")
            click.echo(f"      source:       {p['source'] or '(unknown)'}")
            click.echo(f"      base_url:     {p['base_url'] or '(unknown)'}")
        click.echo()
    click.echo("API keys in env:")
    for env_var, present in snapshot["api_keys_present"].items():
        click.echo(f"  {env_var}: {'set' if present else 'unset'}")
    return snapshot


def auth_refresh(provider: str | None = None) -> list[str]:
    """Force a token refresh now."""
    store = load_auth_store()
    candidates = list((store.get("providers") or {}).keys())
    if provider:
        if provider not in candidates:
            raise AuthError(
                f"No stored credentials for provider {provider!r}.",
                provider=provider,
                code="auth_refresh_missing",
            )
        candidates = [provider]
    refreshed: list[str] = []
    for pid in candidates:
        if pid not in {"openai-chatgpt", "xai-oauth"}:
            continue
        try:
            result = resolve_runtime_credentials(pid, force_refresh=True)
        except AuthError as exc:
            click.echo(f"{pid}: refresh failed — {format_auth_error(exc)}")
            continue
        click.echo(f"{pid}: refreshed (token tail {redact_token(result['access_token'])}).")
        refreshed.append(pid)
    return refreshed


def interactive_picker() -> str:
    """Show the interactive provider picker. Returns the chosen provider id."""
    options = [
        ("openai-chatgpt", "OpenAI (ChatGPT subscription — Plus/Pro/Team/Enterprise)"),
        ("xai-oauth", "xAI Grok (SuperGrok subscription via auth.x.ai)"),
        ("openai", "OpenAI (API key)"),
        ("anthropic", "Anthropic (API key)"),
        ("xai", "xAI Grok (API key)"),
        ("import", "Import existing CLI tokens (~/.codex, ~/.grok)"),
    ]
    click.echo("Pick a provider to log in to:")
    for idx, (_, label) in enumerate(options, start=1):
        click.echo(f"  [{idx}] {label}")
    while True:
        raw = click.prompt(">", default="1").strip()
        try:
            num = int(raw)
        except ValueError:
            click.echo("Please enter a number from the list.")
            continue
        if 1 <= num <= len(options):
            return options[num - 1][0]
        click.echo("Out of range.")
