# Release Notes — v0.6.7

## Theme

`v0.6.7` is a hardening release built on v0.6.6. It makes startup context fail closed instead of leaking raw memory, makes the reflector's input budget configurable and its diagnostics honest, adds an operator-side cap on reflector output that works even on the Codex path, and turns async-Batch provider/billing errors into clean CLI messages. Everything is additive and host-local — no change to existing provider routing, and nothing new is synced through OM Cluster.

## Fail-closed startup hooks

The Claude, Grok, and Cowork `SessionStart` hooks now treat `om context` as the only startup-context producer. When `om context` is unavailable or fails, the hooks emit **no additional agent context** and write a one-line diagnostic to stderr (`observational-memory: om context unavailable — run om doctor`) — they no longer fall back to dumping raw `profile.md` / `active.md` / `reflections.md` / `observations.md`. That raw fallback could inject far more (and unbounded) context than the budgeted path exactly when memory has grown large. A maintainer note in `docs/MAINTAINERS.md` records the invariant so future hooks don't reintroduce it.

## Honest, configurable reflector input budget

The chunked reflector's per-call input ceiling and observation-chunk split are now configurable, and the default no longer silently clamps the configured reflections cap:

- `OM_REFLECTOR_MAX_INPUT_TOKENS` (default `45000`) — the per-call input ceiling, sized so the configured `OM_REFLECTOR_CONTEXT_MAX_CHARS` (default 48000) actually binds instead of being silently overridden by a hardcoded internal limit.
- `OM_REFLECTOR_OBSERVATION_CHUNK_RATIO` (default `0.6`) — fraction of the per-call budget given to the observations chunk; the rest is reflections context.

When the effective reflections cap differs from the configured one, the warning now reports **both** (`configured_reflections_cap` / `effective_reflections_cap` / `max_input_tokens` / `observation_chunk_budget`) so an operator can tell which ceiling is binding. A pathologically small budget that leaves no room for reflections context now sends a marker only — never the full document re-sent past the per-call ceiling. (The deeper 10x/100x scaling redesign — hierarchical / section-targeted reflection — is tracked separately as future work.)

## Reflector output cap (Codex-safe)

`OM_REFLECTOR_OUTPUT_MAX_CHARS` (default `200000`, generous; `0` disables) caps the reflector's emitted document. It is applied post-call in the reflect pipeline, so it bounds **every** backend — including the `openai-chatgpt` (Codex) Responses path, which rejects `max_output_tokens`. When the output overruns, the pipeline trims back to the last complete `## ` section boundary (never mid-section, which would corrupt `reflections.md`) and logs a warning. The reflector prompt also carries an explicit section/line budget so the cap rarely fires.

## Clean async-Batch errors

`om reflect --async` now surfaces OpenAI Batch provider/billing failures (e.g. `billing_hard_limit_reached`, `insufficient_quota`) as a clean one-line CLI error with a non-zero exit instead of a raw Python traceback, on both the submit path and the synchronous fallback. The retry policy is unchanged (non-retryable 400s are still not retried; 429s still are), and a failed submit still leaves no dangling job record and writes nothing to `reflections.md`.

## Configuration

New environment variables (all optional; see `docs/configuration.md`):

```bash
OM_REFLECTOR_MAX_INPUT_TOKENS=45000
OM_REFLECTOR_OBSERVATION_CHUNK_RATIO=0.6
OM_REFLECTOR_OUTPUT_MAX_CHARS=200000
```

## CI

The automatic `Claude Code Review` action on pull-request pushes was removed (it added ~1 minute per cycle without being part of the review process). The interactive `@claude` responder and the ruff + pytest matrix are unchanged.

## Out of scope (follow-ups)

- Hierarchical / section-targeted reflection for 10x and 100x memory scale.
- Obsidian as a backing store / sync target.
- Validating provider/model compatibility before an async Batch submit.

## Validation

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest` (614 tests) all green on the merged `main`.
- HEAD installed from a built wheel and validated on a real machine before release: `om doctor` (36 passed, 0 failures), `om context --quality-report`, fail-closed hooks, the new reflector knobs, and a live async-Batch submission (drift-safety fingerprints recorded).
