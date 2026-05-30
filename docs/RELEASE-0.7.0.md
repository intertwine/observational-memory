# Release Notes — v0.7.0

## Theme

`v0.7.0` makes reflection scale. As `reflections.md` grows, the old reflector rewrote the whole document on every fold — which forced tighter per-call budgets and, at large scale, fell back to re-sending a growing prefix (O(chunks × size)) or truncating to the head, silently dropping older sections. v0.7.0 introduces **section-targeted reflection**: observations are routed to the sections they affect, a compact core bundle rides every fold, only touched sections are patched, and everything else is reassembled byte-for-byte. Markdown stays the user-facing view; the reflector stops re-ingesting the entire document.

This is the first half of the reflector-scaling effort (issue #71). The addressable memory-unit store and hierarchical compaction are deferred to a later release.

## Section-targeted reflection

A new reflector strategy, selected with `OM_REFLECTOR_STRATEGY`:

```bash
OM_REFLECTOR_STRATEGY=auto      # default: small corpora use legacy single-pass/chunked; large corpora use sectioned
OM_REFLECTOR_STRATEGY=legacy    # the prior v0.6.x whole-document reflector
OM_REFLECTOR_STRATEGY=sectioned # always section-targeted
```

How sectioned mode works:

- **Routing.** Each observation chunk is matched to the reflection sections it impacts using deterministic heuristics (headings, repo/project names, paths, keywords) — no extra LLM call.
- **Core bundle.** Core Identity, Preferences & Opinions, Relationship & Communication, Key Facts & Context, the matching Active Project subsection, and Recent Themes (for current work) are always included so the model never loses durable context.
- **Patch, don't rewrite.** The model returns an update for one or a few sections in a strict envelope. Unparseable or invalid output **fails closed** — `reflections.md` is left unchanged and a diagnostic is logged; the reflector never writes a partial or corrupt document.
- **Byte-preserving reassembly.** Untouched sections are preserved exactly; section order, the timestamp prelude, and OM metadata comments are kept; timestamps are updated programmatically.

The result: per-fold reflector input scales with the *touched* sections plus the core bundle, not with the full size of `reflections.md` — eliminating the O(chunks × size) resend at 10x and 100x scale.

## Scaling contract tests

v0.7.0 also lands deterministic 2x/10x/100x scale fixtures and a resend-complexity guard (`tests/test_reflector_scale.py`, `tests/_scale_fixtures.py`). These encode the scaling contract as executable tests — per-call prompt size stays under budget, the binding limit is reported, and large-scale reflection must be section-targeted rather than head-only or full-document. They guard against future regressions that would reintroduce unbounded resend.

## Compatibility

- Default `auto` leaves small-corpus behavior on the legacy path, so most users see no change beyond improved large-corpus scaling.
- `OM_REFLECTOR_STRATEGY=legacy` reproduces the prior v0.6.x behavior exactly.
- The v0.6.7 budget knobs (`OM_REFLECTOR_MAX_INPUT_TOKENS`, `OM_REFLECTOR_OBSERVATION_CHUNK_RATIO`) and the output cap (`OM_REFLECTOR_OUTPUT_MAX_CHARS`) still apply.
- `reflections.md`, `profile.md`, and `active.md` keep the same readable shape.

## Out of scope (follow-ups, → v0.8.0+)

- Addressable memory-unit store (typed units with provenance) and the LLM-as-proposal-engine pipeline.
- Hierarchical compaction (evidence-preserving summaries, archive tiers) for unbounded growth.
- Tracked in issue #71.

## Validation

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest` all green on the merged `main`.
- Section-targeted reflection passed a 5-lens adversarial Gate-3 review (misrouting, silent memory loss, fail-closed, contract integrity, strategy compatibility); 16 findings raised and fixed.
- Safety is directly tested: byte-identical round-trip reassembly, single-section updates preserving unrelated sections byte-for-byte, and invalid model output leaving memory unchanged.
