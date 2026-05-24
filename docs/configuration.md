# Configuration

Most users only need `om install`. This page explains the knobs behind it.

## Env File

The main config file is:

```bash
~/.config/observational-memory/env
```

On Windows:

```text
%APPDATA%\observational-memory\env
```

`om install` creates this file with owner-only permissions. The CLI loads it at startup, including when hooks and scheduled jobs call `om`.

Environment variables already set in your shell win over values in the file.

## Save Money: Use Your Subscription

If you already pay for ChatGPT Plus / Pro / Team / Enterprise or for SuperGrok, you can point `om` at that subscription instead of an API key. Observations and reflections then ride on a plan you already paid for, with no per-token meter.

| Provider           | Auth                       | Default model       | Marginal cost per call |
|--------------------|----------------------------|---------------------|------------------------|
| `openai-chatgpt`   | ChatGPT subscription OAuth | `gpt-5.5`           | $0 (your plan)         |
| `xai-oauth`        | SuperGrok OAuth (PKCE)     | `grok-code-fast-1`  | $0 (your plan)         |
| `xai`              | `XAI_API_KEY`              | `grok-code-fast-1`  | Metered                |
| `openai`           | `OPENAI_API_KEY`           | `gpt-4o-mini`       | Metered                |
| `anthropic`        | `ANTHROPIC_API_KEY`        | `claude-sonnet-4-5` | Metered                |

To sign in, run `om login` and pick your provider. Tokens land in `~/.config/observational-memory/auth.json` (0600, host-local). `om` never writes back to `~/.codex/` or `~/.grok/`; if you already have those CLIs, run `om login --import` to copy their tokens into om's own store.

`om auth status` shows what is currently configured (tokens are redacted to the last 4 characters). `om auth refresh` forces a refresh now. `om logout [provider]` clears stored tokens.

## Provider Settings

Direct Anthropic:

```bash
OM_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
OM_LLM_MODEL=claude-sonnet-4-5-20250929
```

Direct OpenAI:

```bash
OM_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OM_LLM_MODEL=gpt-4o-mini
```

OpenAI ChatGPT subscription (Plus / Pro / Team / Enterprise):

```bash
OM_LLM_PROVIDER=openai-chatgpt
OM_OPENAI_CHATGPT_MODEL=gpt-5.5
# Optional overrides:
# OM_OPENAI_CHATGPT_BASE_URL=https://chatgpt.com/backend-api/codex
# OM_OPENAI_CHATGPT_CLIENT_ID=app_EMoamEEZ73f0CkXaXp7hrann
```

Tokens come from `om login openai-chatgpt` (OAuth device-code against `https://auth.openai.com`). Calls route to the Codex backend at `https://chatgpt.com/backend-api/codex`. This backend is **not** a plain Chat Completions endpoint — `om` talks to it via the **Responses API** (`/responses`) with the streaming, `store=false` request shape the Codex CLI uses, and sends Cloudflare-clearing headers (`originator: codex_cli_rs`, a `codex_cli_rs` User-Agent, and `ChatGPT-Account-ID` from your token). Refresh happens automatically when the cached token is within 120 seconds of expiry, plus once on any 401 response.

The set of models the Codex backend accepts for ChatGPT-account auth is an undocumented, shifting allow-list. As of 2026-05-23 it included `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, and `gpt-5.2`; `gpt-5-codex` was **not** accepted. The default is `gpt-5.5`; set `OM_OPENAI_CHATGPT_MODEL` if the allow-list moves and you see an HTTP 400 "model is not supported". `max_tokens` is not forwarded to this backend (it rejects the parameter).

xAI Grok subscription (SuperGrok):

```bash
OM_LLM_PROVIDER=xai-oauth
OM_XAI_OAUTH_MODEL=grok-code-fast-1
# Optional overrides:
# OM_XAI_OAUTH_BASE_URL=https://api.x.ai/v1
# OM_XAI_OAUTH_CLIENT_ID=b1a00492-073a-47ea-816f-4c329264a828
# OM_XAI_OAUTH_REDIRECT_PORT=56121
# OM_XAI_OAUTH_TIMEOUT_SECONDS=300
```

Tokens come from `om login xai-oauth` (loopback authorization-code + PKCE against `https://auth.x.ai`). The flow ports the upstream Hermes implementation verbatim (`nousresearch/hermes-agent` `hermes_cli/auth.py` blob `5fd3676`, 2026-05-23), including:

- `plan=generic` + `referrer=observational-memory` on the authorize request
- S256 PKCE with the `code_challenge` echoed at the token step (xAI's #26990 quirk)
- a manual-paste fallback for SSH / Cloud Shell / Codespaces (`om login xai-oauth --manual-paste`)
- `*.x.ai` host pinning on the discovered endpoints **and** the inference base URL — a tampered `OM_XAI_OAUTH_BASE_URL` cannot exfiltrate the bearer
- HTTP 403 from the token endpoint maps to `xai_oauth_tier_denied` with a clear hint to switch to `OM_LLM_PROVIDER=xai` + `XAI_API_KEY`

xAI Grok with an API key (metered fallback):

```bash
OM_LLM_PROVIDER=xai
XAI_API_KEY=xai-...
OM_XAI_MODEL=grok-code-fast-1
# Optional:
# OM_XAI_BASE_URL=https://api.x.ai/v1
```

Anthropic on Vertex AI:

```bash
OM_LLM_PROVIDER=anthropic-vertex
OM_VERTEX_PROJECT_ID=my-gcp-project
OM_VERTEX_REGION=us-east5
OM_LLM_MODEL=claude-sonnet-4-5-20250929
```

Anthropic on Bedrock:

```bash
OM_LLM_PROVIDER=anthropic-bedrock
OM_BEDROCK_REGION=us-east-1
OM_LLM_MODEL=anthropic.claude-sonnet-4-5-20250929-v1:0
```

`OM_LLM_PROVIDER=auto` (the default) resolves providers in this order:

1. `anthropic` if `ANTHROPIC_API_KEY` is set
2. `openai` if `OPENAI_API_KEY` is set
3. `openai-chatgpt` if `om login openai-chatgpt` tokens exist
4. `xai-oauth` if `om login xai-oauth` tokens exist
5. `xai` if `XAI_API_KEY` is set

Existing API-key users see no behavior change. New users discover the subscription paths via `om install`, `om login`, and `om doctor`.

Model precedence:

1. `OM_LLM_OBSERVER_MODEL` or `OM_LLM_REFLECTOR_MODEL`
2. `OM_LLM_MODEL`
3. provider default

### Different providers per workflow

The observer runs often (hooks, schedulers) and suits a fast, cheap model; the reflector runs rarely and suits a stronger one. Pin a provider per workflow:

```bash
OM_LLM_OBSERVER_PROVIDER=xai-oauth      # fast model for frequent observe
OM_LLM_REFLECTOR_PROVIDER=openai-chatgpt # strong model for durable reflect
```

When a per-workflow provider is set, that workflow uses it directly (no model-name inference), and its model resolves from the per-step override (`OM_LLM_OBSERVER_MODEL` / `OM_LLM_REFLECTOR_MODEL`) or that provider's default — **not** the global `OM_LLM_MODEL`, which usually belongs to a different provider.

### Observer context budget

Every observe run re-sends part of `observations.md` for dedup context. `OM_OBSERVER_CONTEXT_MAX_CHARS` (default `12000`) caps how much of the recent tail is sent so input cost doesn't grow with the file. Set it to `0` to send the whole file.

This cap only takes effect when OM Cluster is enabled, where observations are an append-only record log and `observations.md` is a materialized view — a bounded context can't lose history. In non-cluster mode the observer rewrites the whole file, so the full existing content is always sent regardless of this setting.

### Reflector context budget

The reflector folds new observations into `reflections.md`. For large observation sets it works in chunks, re-sending the running document on each fold — without a bound that cost grows with the number of chunks. `OM_REFLECTOR_CONTEXT_MAX_CHARS` (default `32000`) caps how much of `reflections.md` is re-sent as context. The default is generous — a target-size reflections file (the prompt aims for 200–600 lines) is always sent whole, so the cap only trims unusually large documents. Set it to `0` to disable the bound.

When the cap does trim, it keeps the head of the document (durable identity and active projects sit at the top) and logs a warning. It bounds only the *input* context — the reflector still emits a complete document — so a normal run never shrinks your stored memory. If you see the warning, raise the cap or let the reflector compress the file.

### Latency: Codex reasoning effort

ChatGPT Codex (`openai-chatgpt`) accepts a reasoning effort — `low`, `medium`, `high`, or `xhigh`. Lower effort cuts `gpt-5.5` latency sharply. Observe runs default to `low` (it's frequent and latency-sensitive); reflect is left at the backend default to protect consolidation quality. Override globally or per operation:

```bash
OM_OPENAI_CHATGPT_REASONING_EFFORT=low            # all Codex calls
OM_OPENAI_CHATGPT_OBSERVER_REASONING_EFFORT=low   # observe only (default)
OM_OPENAI_CHATGPT_REFLECTOR_REASONING_EFFORT=medium  # reflect only
```

Unrecognized values are ignored (the backend default is used), so a typo can't fail a call.

### Prompt caching

On the metered Anthropic providers (`anthropic`, `anthropic-vertex`, `anthropic-bedrock`), the stable observer/reflector system prompt is sent as a cacheable block (`cache_control: ephemeral`), so repeat calls reuse it at a fraction of the input cost. OpenAI and xAI cache eligible prefixes automatically — no configuration needed. The ChatGPT Codex backend does not expose cache controls, so its instructions are sent as-is.

### Seeing what will run

`om status` and `om auth status` both show the resolved provider, the model each workflow will use, your stored subscription tokens (redacted), and a warning when subscription tokens exist but `auto` resolution is still using a metered API key (set `OM_LLM_PROVIDER` or re-run `om login` to fix).

## Usage, Cost, and Budgets

Observational Memory records every LLM call so you can see what `observe` and `reflect` actually cost, and stop a runaway job before it burns a budget. This is host-local: the data lives in `usage.sqlite` next to your memory files and is never synced through OM Cluster.

### What gets recorded

Every call through the LLM layer writes one row: timestamp, provider, model, operation (`observer` / `reflector`), prompt/completion tokens, an estimated USD cost, latency, retries, status, and the repo it ran in. Subscription-backed calls (`openai-chatgpt`, `xai-oauth`) record their tokens but cost `$0.00` — they are paid for by your flat subscription.

Token counts come straight from the provider response. The ChatGPT Codex streaming path reports usage on its final event; when a provider gives no usage object, OM falls back to a `chars/4` estimate (marked `token_source=estimate`).

```bash
om usage status                 # totals, budgets, and pricing snapshot on one screen
om usage status --since 2026-05-01 --json
om usage tail --limit 20        # the most recent calls, newest first
```

Turn tracking off entirely with `OM_USAGE_TRACKING=0` — no database is created and the only overhead is a single env check.

### Budgets

Budgets are user-side guardrails. Declare them with the wizard or set them directly; they are stored in your env file.

```bash
om usage budget                              # interactive: scope, window, caps, hard/soft
om usage budget set --daily-usd 5.00
om usage budget set --operation reflector --daily-usd 1.00 --soft
om usage budget set --monthly-tokens 5_000_000
om usage budget clear --operation reflector
```

A budget is named `OM_BUDGET_[<OPERATION>_]<WINDOW>_<UNIT>`:

- `OPERATION` (optional): `OBSERVER` or `REFLECTOR`; omit for a global cap.
- `WINDOW`: `DAILY`, `MONTHLY`, or `SESSION` (one `om` process).
- `UNIT`: `USD` or `TOKENS` (enforced independently).

| Variable | Meaning |
| --- | --- |
| `OM_BUDGET_DAILY_USD=5.00` | $5/day across all operations |
| `OM_BUDGET_REFLECTOR_DAILY_USD=1.00` | $1/day for reflect only |
| `OM_BUDGET_DAILY_TOKENS=2_000_000` | 2M tokens/day |
| `OM_BUDGET_MODE=hard` | `hard` blocks; `soft` warns. Per-budget override: `<KEY>_MODE` |
| `OM_BUDGET_SOFT_THRESHOLD=0.8` | warn once spend reaches 80% of a cap |
| `OM_BUDGET_BYPASS=1` | one-shot escape hatch for a single call |

Before each call, OM estimates its cost (prompt `chars/4` plus the requested output cap) and checks it against current spend. A **hard** cap refuses the call with a clear message; a **soft** cap proceeds but warns. To push one call through a hard cap, prefix it: `OM_BUDGET_BYPASS=1 om reflect …`. `recall` makes no LLM call today, so it carries no budget.

A model with no price (not in the snapshot or your overrides) has no dollar estimate, so a **USD** cap can't gate it — use a **token** cap (enforced from token counts regardless of pricing) if you want a hard ceiling on unpriced models.

### Pricing

Cost estimates use a dated pricing snapshot shipped in the package. Override any model per host — overrides win and are easy to keep current.

```bash
om usage pricing show                                   # effective table + snapshot date
om usage pricing set --model gpt-5.5 --input 1.25 --output 10.00   # USD per 1M tokens
om usage pricing reset                                  # drop overrides
```

Overrides live at `~/.config/observational-memory/pricing.toml` (set `OM_PRICING_OVERRIDES` to relocate). Unknown models record token counts with `pricing=unknown` and skip the dollar estimate. `om doctor` reports the tracking state, configured budgets, and the active pricing snapshot.

## Auth Store

`om login` writes a single host-local file:

```text
~/.config/observational-memory/auth.json   # POSIX
%APPDATA%\observational-memory\auth.json   # Windows
```

The file is created `0600` on POSIX, sits next to the existing env file, is guarded by a cross-process file lock, and never enters OM Cluster sync. Override the location for tests or experiments with `OM_AUTH_FILE=/tmp/auth.json`.

## Memory Paths

Default local memory:

```bash
~/.local/share/observational-memory/
```

Windows default:

```text
%LOCALAPPDATA%\observational-memory\
```

Override with XDG paths:

```bash
export XDG_DATA_HOME=~/my-data
export XDG_CONFIG_HOME=~/my-config
```

Important files:

- `observations.md`: recent notes
- `reflections.md`: long-term memory
- `profile.md`: stable startup context
- `active.md`: current startup context
- `.cursor.json`: transcript checkpoints
- `.search-index/`: local search index

## Startup Controls

Control generated profile sections:

```bash
OM_PROFILE_INCLUDE_IDENTITY=0
OM_PROFILE_SECTIONS=preferences,relationship,key-facts
```

These settings only narrow generated profile/startup output. They do not turn off observation, reflection, search, recall, or cluster sync.

## Reflection Metadata

Reflection entries use inline comments like:

```markdown
- Prefer short status updates <!--om: id=ome_abc kind=preference actionability=medium scope=cluster-->
```

Common fields:

- `kind`: `snapshot`, `evergreen`, `preference`, `policy`, `identity`, `task`, `decision`, or `mode`
- `actionability`: `low`, `medium`, or `high`
- `sensitivity`: `normal` or `personal`
- `confidence`: usually `medium`
- `scope`: `cluster` or `local`
- `last_seen`, `last_verified`, `expires`, and `seen_count`

Unknown fields are preserved.

## Schedules

Default schedules:

- Codex observer backstop: every 15 minutes
- Claude auto-memory scan: hourly
- reflector: daily at 04:00 local time

Tune Codex polling:

```bash
OM_CODEX_OBSERVER_INTERVAL_MINUTES=10
```

Tune in-session checkpoints:

```bash
OM_SESSION_OBSERVER_INTERVAL_SECONDS=900
OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=0
```

## Search Backend

Default:

```bash
OM_SEARCH_BACKEND=bm25
```

Optional QMD:

```bash
OM_SEARCH_BACKEND=qmd-hybrid
OM_QMD_INDEX_NAME=observational-memory
OM_QMD_NO_RERANK=1
```

See [search-and-recall.md](search-and-recall.md) for QMD setup.

## Cluster Flags

Cluster mode is off until local cluster config and keys exist.

Force cluster off for one command:

```bash
OM_CLUSTER_ENABLED=0 om context
```

Useful cluster env overrides:

```bash
OM_CLUSTER_ENABLED=1
OM_CLUSTER_SYNC_BEFORE_CONTEXT=1
OM_CLUSTER_STARTUP_PULL_DEADLINE_MS=1500
```

Relay and filesystem transports remain untrusted. Cluster trust comes from local keys, signatures, membership records, and approval state.
