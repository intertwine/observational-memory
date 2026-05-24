# Release Notes — v0.6.6

## Theme

**Know what memory costs, make it cheaper, and keep it trustworthy.**

`v0.6.6` is a feature release built on the v0.6.5 subscription-auth work. It adds a host-local cost/budget subsystem, an offline (Batch) path for the expensive reflection step, observe/reflect cost-and-latency improvements, and startup-context quality controls. Everything is additive and host-local — no change to existing provider behavior, and nothing new is synced through OM Cluster.

## Usage, cost & budgets (`om usage`)

Every LLM call routed through `om` now records one row to a host-local SQLite database (`usage.sqlite`, next to your memory files, never synced): provider, model, operation, prompt/completion tokens, estimated USD cost, latency, and repo. Subscription-backed calls (`openai-chatgpt`, `xai-oauth`) record tokens at `$0.00`.

```bash
om usage status [--since YYYY-MM-DD] [--json]   # totals, budgets, pricing snapshot
om usage tail [--limit N]                       # recent calls, newest first
om usage budget                                 # interactive wizard
om usage budget set --daily-usd 5.00 [--operation reflector] [--soft]
om usage budget clear --operation reflector | --all
om usage pricing show | set --model M --input X --output Y | reset
```

Budgets are `OM_BUDGET_[<OPERATION>_]<WINDOW>_<UNIT>` (USD and tokens; day/month/session; global or per-operation; hard or soft). A **hard** cap refuses the next call *before* it bills (with a documented `OM_BUDGET_BYPASS=1` one-shot override); a **soft** cap warns. Pricing comes from a dated, in-repo snapshot (`pricing.toml`) overridable per host. `OM_USAGE_TRACKING=0` disables the whole subsystem. Surfaced from `om doctor`.

## Offline reflection via OpenAI Batch (`om reflect --async`, `om jobs`)

For the direct API-key `openai` provider, a single-pass reflection can run offline through the OpenAI Batch API (24-hour window, ~50% token cost, separate rate pool):

```bash
OM_LLM_REFLECTOR_PROVIDER=openai
om reflect --async        # submit a Batch job and exit
om jobs poll              # apply completed jobs (drift-checked)
om jobs list | show <id> | cancel <id>
```

Or set `OM_OPENAI_ASYNC_MODE=batch` for scheduled runs. Batch is **never** used for the `openai-chatgpt` subscription provider (no Batch API) — it errors clearly. Results map by `custom_id`; state advances only on apply; and apply refuses (saving a review artifact instead) if `reflections.md`, the new observations, or auto-memory changed since submit, so a late result never clobbers newer memory.

## Cost & latency for observe/reflect

- **Bounded reflector input** — `OM_REFLECTOR_CONTEXT_MAX_CHARS` (default 48000) caps the reflections context re-sent on each chunked fold, fixing an O(chunks × size) re-send.
- **Codex reasoning effort** — `OM_OPENAI_CHATGPT_REASONING_EFFORT` (+ `_OBSERVER_`/`_REFLECTOR_`), forwarded to the Responses API. Default `low` for observe; reflect keeps the backend default to protect consolidation quality.
- **Anthropic prompt caching** — the stable system prompt is sent as a `cache_control: ephemeral` block; OpenAI/xAI cache automatically.

## Startup context quality

As the corpus grows, `om context` now applies three quality passes: cross-section **de-duplication** (repeated profile guidance shown once; per-project fields kept distinct), **freshness** markers on operational facts older than `OM_STARTUP_FRESHNESS_DAYS` (default 14; durable facts never marked), and cwd/task-aware **scope** (the current project gets first budget claim; the rest overflows to recall handles). Inspect it all with `om context --quality-report [--json]`.

## Configuration

All new env vars and commands are documented in [`docs/configuration.md`](configuration.md) and seeded (commented) into the env-file template. The OpenAI Batch live-smoke procedure is in [`docs/MAINTAINERS.md`](MAINTAINERS.md).

## Out of scope (follow-ups)

- Obsidian as a backing store / sync target (#54) ships in a later release.
- OpenAI Responses *background* mode and Flex processing (alternatives to Batch).
- Cross-cluster usage aggregation (usage stays host-local in v1).

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest          # full suite green
make verify-session-start
```

The OpenAI Batch path is fully covered by mocked tests; a live end-to-end smoke is opt-in and documented in `docs/MAINTAINERS.md` (it was blocked during development by an account billing hard-limit).
