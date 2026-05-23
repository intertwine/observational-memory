# Release Notes — v0.6.5

## Theme

**Stop paying per-token when you already pay for a subscription.**

`v0.6.5` adds first-class `om login` flows for the two ecosystems that publish a subscription-backed inference path:

- **OpenAI ChatGPT** (Plus / Pro / Team / Enterprise) via the Codex OAuth device-code flow against `https://auth.openai.com`, routed to `https://chatgpt.com/backend-api/codex`.
- **xAI Grok** (SuperGrok) via the OIDC authorization-code + PKCE **loopback** flow against `https://auth.x.ai`, routed to `https://api.x.ai/v1`.

Both flows are also reachable as zero-prompt imports if you already have Codex CLI or Grok CLI installed (`om login --import`). All existing API-key paths (`anthropic`, `openai`, plus a new metered `xai`) behave identically to v0.6.4.

## New providers

| Provider id        | Transport                                                          | Auth                              | Default model         |
|--------------------|--------------------------------------------------------------------|-----------------------------------|-----------------------|
| `openai-chatgpt`   | Codex **Responses API**, `base_url=chatgpt.com/backend-api/codex`  | OAuth (subscription, device-code) | `gpt-5.5`             |
| `xai-oauth`        | OpenAI-compatible Chat Completions, `base_url=api.x.ai/v1`         | OIDC loopback + PKCE              | `grok-code-fast-1`    |
| `xai`              | OpenAI-compatible Chat Completions, `base_url=api.x.ai/v1`         | `XAI_API_KEY` (metered fallback)  | `grok-code-fast-1`    |

> Note: the `openai-chatgpt` transport/model row reflects what live testing
> proved (see the E2E section) — the Codex backend is the streaming Responses
> API behind Cloudflare, not Chat Completions, and `gpt-5-codex` is not on the
> ChatGPT-account allow-list. The original plan assumed otherwise; corrected in
> the plan amendment.

`OM_LLM_PROVIDER=auto` resolves in this order, so today's users see no change:

1. `anthropic` if `ANTHROPIC_API_KEY` is set
2. `openai` if `OPENAI_API_KEY` is set
3. `openai-chatgpt` if `om login openai-chatgpt` tokens exist
4. `xai-oauth` if `om login xai-oauth` tokens exist
5. `xai` if `XAI_API_KEY` is set

## New CLI

```
om login                               # interactive provider picker
om login openai-chatgpt                # device-code flow
om login xai-oauth [--manual-paste]    # loopback PKCE flow
om login --import                      # zero-prompt import from ~/.codex/, ~/.grok/
om login --api-key {openai|anthropic|xai} [--key KEY]
om logout [provider]                   # clear om-owned tokens; defaults to all
om auth status [--json]                # provider, expiry, source, redacted token tail
om auth refresh [provider]             # force a refresh now (diagnostic)
```

`om install` now mentions the subscription paths first. `om doctor` reports auth-store presence, file permissions (must be `0600`), and warns when stored tokens expire within 24 hours.

## Auth store

`om login` writes a single file:

```
~/.config/observational-memory/auth.json   (POSIX, 0600)
%APPDATA%\observational-memory\auth.json   (Windows)
```

The file is created next to the existing `env` file, guarded by a cross-process file lock (`fcntl` on POSIX, `msvcrt` on Windows, 10s timeout), and written atomically via `O_EXCL` to avoid TOCTOU leaks at default umask. Tokens are never printed to stdout or logs; `om auth status` redacts to the last 4 characters.

`om` never writes to `~/.codex/auth.json` or `~/.grok/auth.json` — the importer is read-only.

## Security model

The xAI loopback flow ports the upstream Hermes implementation (`nousresearch/hermes-agent` `hermes_cli/auth.py` blob `5fd3676`, 2026-05-23) verbatim, including:

- `plan=generic` + `referrer=observational-memory` on the authorize request (without `plan=generic`, accounts.x.ai rejects loopback OAuth from non-allowlisted clients)
- S256 PKCE with the `code_challenge` echoed at the token step — defense-in-depth for the xAI `code_challenge is required` quirk reported in upstream issue #26990
- a CORS handler on the loopback server that only accepts `accounts.x.ai` / `auth.x.ai` origins, so the consent screen's success script can dismiss the tab
- manual-paste fallback for SSH / Cloud Shell / Codespaces (`--manual-paste`)
- `*.x.ai` host pinning on the discovery endpoints **and** the inference base URL, so a tampered `XAI_BASE_URL` cannot exfiltrate the bearer
- HTTP 403 from the token endpoint maps to `xai_oauth_tier_denied` with a clear hint to switch to `XAI_API_KEY` + `OM_LLM_PROVIDER=xai`

The OpenAI ChatGPT device-code flow ports `_codex_device_code_login` and `refresh_codex_oauth_pure` from the same upstream file. Refresh-token rotation is handled per OpenAI's rules; `refresh_token_reused` (raised when Codex CLI / VS Code rotated the same refresh token) surfaces with a clear "re-run `om login openai-chatgpt`" message.

The cited upstream blob SHA appears in each ported module's header comment so future maintainers can pull bug fixes.

## Risks and mitigations

- **Client-id authorization drift** — both providers may revoke the Codex CLI / Grok CLI client ids. Env-var overrides (`OM_OPENAI_CHATGPT_CLIENT_ID`, `OM_XAI_OAUTH_CLIENT_ID`) let users swap them without a code change.
- **xAI subscription tier denial** — base-tier SuperGrok accounts cannot grant `grok-cli:access`. The 403 is caught and mapped to `xai_oauth_tier_denied` with a metered-fallback recommendation.
- **Discovery cache poisoning** — every read of a cached `token_endpoint` is re-validated against `*.x.ai`; a hand-edited `auth.json` cannot redirect refreshes off-domain.
- **Token theft surface area** — `auth.json` is `0600`, tokens never reach stdout / logs, refresh tokens are scrubbed from quarantined entries on terminal failures.

## Out of scope (follow-ups)

- Token-budget tracking and per-call cost reports (#51 — independent).
- Anthropic Claude Pro / Max subscription auth (Anthropic does not currently publish a third-party-client inference endpoint).
- Cluster-syncing tokens (auth state stays host-local).
- First-party `observational-memory` OAuth client registration with OpenAI / xAI.

## Validation

```bash
make check                  # ruff check + ruff format --check + pytest
uv run pytest tests/auth/   # focused suite for the new module
```

Run the new commands without an account first to exercise the error paths:

```bash
uv run om auth status
uv run om logout            # idempotent when nothing is stored
uv run om login --import    # safely reports "Nothing to import"
```

## E2E proof — live subscription transcript

> **Captured by**: the maintainer, against their own subscription accounts
> **Host**: macOS (hostname redacted to `<host>`), om installed from this branch
> **Date**: 2026-05-23
>
> Live `om login openai-chatgpt` and live `om login xai-oauth` against the
> maintainer's own subscription accounts, followed by one `om observe` and one `om reflect`
> run with each provider active. Token tails are redacted to the last 4 chars;
> the one-time device code shown during login is single-use and already expired.

### ChatGPT subscription

```text
$ uv run om login openai-chatgpt
To sign in, follow these steps:

  1. Open this URL in your browser:
     https://auth.openai.com/codex/device

  2. Enter this code:
     HKEW-BP3OG          # one-time, already consumed

Waiting for sign-in... (press Ctrl+C to cancel)
Notice: by logging in, you confirm that om's use of your subscription complies with the provider's terms of service.

Signed in as ChatGPT subscriber.
Wrote tokens to ~/.config/observational-memory/auth.json
Next: try `om observe` — om will use your ChatGPT subscription.

$ uv run om auth status
Active provider: openai-chatgpt

Subscription providers (from auth.json):
  - openai-chatgpt   OpenAI ChatGPT subscription
      access_token: ****0dQg
      expires_at:   2026-06-02T10:43:14Z
      last_refresh: 2026-05-23T10:43:14.522253Z
      source:       device-code
      base_url:     https://chatgpt.com/backend-api/codex

API keys in env:
  OPENAI_API_KEY: set
  ANTHROPIC_API_KEY: set
  XAI_API_KEY: unset
```

The one-time ToS notice fires on first login only. Note that `OPENAI_API_KEY`
and `ANTHROPIC_API_KEY` are both present in the environment, so `auto` would
pick `anthropic`; we set `OM_LLM_PROVIDER=openai-chatgpt` explicitly to force
the subscription path for the proof.

Getting a real `om observe` through the ChatGPT subscription took five rounds of
debugging the Codex backend — captured here because each fix is now in the code
and the plan amendment:

1. The first run silently used the **metered** `openai` API: `OM_LLM_MODEL=gpt-5.5`
   in the env, combined with `_infer_provider`'s `gpt-*` → `openai` rule, routed
   the call away from the subscription. Fixed by making subscription providers
   sticky in `_infer_provider` (regression test added).
2. With the subscription path forced, `POST .../chat/completions` returned a
   **Cloudflare challenge page**. Fixed with the ported `cloudflare_headers`
   (`originator: codex_cli_rs`, codex User-Agent, `ChatGPT-Account-ID` from the JWT).
3. Cloudflare cleared → **HTTP 404**: the Codex backend speaks the **Responses API**,
   not Chat Completions. Switched to `client.responses.create(...)`.
4. The Responses call needed `input` as a message **list** (not a string), then
   `store=false`, then `stream=true` — three sequential HTTP 400s, each fixed.
5. `gpt-5-codex` returned 400 "model is not supported"; the live `/models`
   endpoint listed `gpt-5.5` / `gpt-5.4` / `gpt-5.3-codex`. Default changed to
   `gpt-5.5`. `max_output_tokens` is also rejected and is no longer sent.

```text
$ OM_LLM_PROVIDER=openai-chatgpt OM_LLM_MODEL=gpt-5.5 uv run om observe --source claude
# Real gpt-5.5 call via the Codex Responses API through the ChatGPT subscription.
# observations.md grew 6509 -> 6749 lines (+240).
```

The +240-line delta is the proof the call landed on the subscription. The new
record carried the expected structure (a `### From <host> / claude / <project>`
header, a `### Current Context` block, and a `### Observations` list with
🔴/🟡 priority markers). The raw observation text is the maintainer's own private
memory and is intentionally not reproduced here (repo doc rule); the line-count
delta, command, and redacted token tail above are the evidence.

```text
$ OM_LLM_PROVIDER=openai-chatgpt OM_LLM_MODEL=gpt-5.5 uv run om reflect
Running reflector...
Reflections updated (121026 chars)
```

A full gpt-5.5 reflection through the ChatGPT subscription: `reflections.md` was
rewritten 186 → 400 lines (`*Last updated: 2026-05-23 12:00 UTC*`), with a fresh
`### Observational Memory v0.6.5 Account Auth / Cost Reduction` project section and
correct inline `<!--om: ...-->` metadata on every entry. This is the genuine
subscription-backed reflect run (no metered API key involved).

### SuperGrok subscription

```text
$ uv run om login xai-oauth
Open this URL to authorize om with xAI:
https://auth.x.ai/oauth2/authorize?response_type=code&client_id=b1a00492-073a-47ea-816f-4c329264a828&redirect_uri=http%3A%2F%2F127.0.0.1%3A56121%2Fcallback&scope=openid+profile+email+offline_access+grok-cli%3Aaccess+api%3Aaccess&code_challenge=<REDACTED>&code_challenge_method=S256&state=<REDACTED>&nonce=<REDACTED>&plan=generic&referrer=observational-memory

Waiting for callback on http://127.0.0.1:56121/callback
Browser opened for xAI authorization.

Signed in to xAI Grok (SuperGrok).
Wrote tokens to ~/.config/observational-memory/auth.json
Next: try `om observe` with OM_LLM_PROVIDER=xai-oauth.
```

The authorize URL is the real one: note `plan=generic`, `referrer=observational-memory`,
`code_challenge_method=S256`, and the full `grok-cli:access api:access` scope set.
The `code_challenge`/`state`/`nonce` values are single-use per-login and redacted here.

```text
$ uv run om auth status
Active provider: xai-oauth

Subscription providers (from auth.json):
  - openai-chatgpt   OpenAI ChatGPT subscription
      access_token: ****0dQg
      expires_at:   2026-06-02T10:43:14Z
      last_refresh: 2026-05-23T10:43:14.522253Z
      source:       device-code
      base_url:     https://chatgpt.com/backend-api/codex
  - xai-oauth        xAI Grok (SuperGrok subscription)
      access_token: ****GEwA
      expires_at:   2026-05-23T17:14:35Z
      last_refresh: 2026-05-23T11:14:35.078879Z
      source:       loopback-pkce
      base_url:     https://api.x.ai/v1

API keys in env:
  OPENAI_API_KEY: set
  ANTHROPIC_API_KEY: set
  XAI_API_KEY: unset
```

Both subscription providers now coexist in one `auth.json`. The xAI access
token carries a ~6h expiry (vs. the ChatGPT token's ~10 days); the 120s refresh
skew and the 401-triggered single retry keep scheduled jobs from blocking on it.

Unlike the Codex backend, `api.x.ai/v1` is a genuine OpenAI-compatible Chat
Completions endpoint, so the xAI path works through the shared client with no
special headers. (Note: a global `OM_LLM_MODEL=gpt-5.5` is not a valid xAI
model, so we pass `grok-code-fast-1` explicitly; the subscription-sticky routing
fix keeps the call on `xai-oauth` instead of bouncing it to metered OpenAI.)

```text
$ OM_LLM_PROVIDER=xai-oauth OM_LLM_MODEL=grok-code-fast-1 uv run om observe --source claude
# Real grok-code-fast-1 call through the SuperGrok subscription (api.x.ai/v1).
# observations.md grew 6322 -> 6359 lines (+37).
```

Same as the ChatGPT run: the +37-line delta is the proof. The new record had the
expected structure; the raw text (the maintainer's private memory) is not
reproduced here.

The first `xai-oauth` reflect printed `No observations to reflect on.` — the
ChatGPT reflect had already consumed the pending observations, so grok correctly
short-circuited before making a call. After one more `om observe --source claude`
(grok) added fresh observations, the reflect made a real grok call:

```text
$ OM_LLM_PROVIDER=xai-oauth OM_LLM_MODEL=grok-code-fast-1 uv run om observe --source claude
Scanning Claude Code transcripts...
Processed 1 transcript(s)

$ OM_LLM_PROVIDER=xai-oauth OM_LLM_MODEL=grok-code-fast-1 uv run om reflect
Running reflector...
Reflections updated (75935 chars)
```

A full grok-code-fast-1 reflection through the SuperGrok subscription:
`reflections.md` was rewritten (`*Last updated: 2026-05-23 12:11 UTC*`), 75,935
characters of model output. (grok produced a more concise reflection than
gpt-5.5 — 243 lines vs. 400 — which is expected model-to-model variation.)

### Summary

| Provider          | Model              | observe                       | reflect                          |
|-------------------|--------------------|-------------------------------|----------------------------------|
| `openai-chatgpt`  | `gpt-5.5`          | observations.md +240 lines    | reflections.md rewrite, 121,026 chars |
| `xai-oauth`       | `grok-code-fast-1` | observations.md +37 lines     | reflections.md rewrite, 75,935 chars  |

Both providers completed real `om observe` **and** `om reflect` runs against
subscription-backed inference, with redacted token tails shown above. No
synthetic mocks were used in this section.

## Release boundary

Per `CLAUDE.md`, this PR **does not** bump the version, tag, publish to PyPI, or update Homebrew. Those steps wait for explicit approval after these release notes are reviewed.
