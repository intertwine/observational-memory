# Plan: QMD 2.1 Integration Upgrade for Observational Memory

## Context

QMD `v2.1.0` was released on 2026-04-05 with several search-facing improvements:

- AST-aware code chunking via `--chunk-strategy auto`
- per-collection `models:` configuration in `index.yml`
- `--no-rerank` for faster hybrid queries
- clickable editor hyperlinks in CLI output
- `line` in JSON search output
- `qmd bench <fixture.json>` for retrieval evaluation
- BM25 and embedding stability fixes

`om` already has an optional QMD backend, but the current adapter is intentionally thin:

- `src/observational_memory/search/qmd.py` writes generated Markdown into `.qmd-docs/`
- it shells out to `qmd update`
- it uses `qmd search` for `qmd` and `qmd query` for `qmd-hybrid`
- it parses `--json` output into `SearchResult`
- it does not expose QMD-specific tuning knobs
- it does not surface QMD status, model selection, file paths, or line numbers

That means `om` benefits automatically from upstream bug fixes after users upgrade QMD, but it does **not** yet leverage the new 2.1 control surface.

## Ground Truth From This Repo

### Current `om` behavior

- `src/observational_memory/search/qmd.py`
  - uses a fixed collection name: `observational-memory`
  - writes one generated `.md` file per parsed `Document`
  - calls `qmd update` after each reindex
  - never calls `qmd embed`
  - does not pass `--no-rerank`, `--chunk-strategy`, or `--index`
- `src/observational_memory/search/__init__.py`
  - maps `qmd` → `QMDBackend(..., mode="search")`
  - maps `qmd-hybrid` → `QMDBackend(..., mode="query")`
- `src/observational_memory/cli.py`
  - `om search` normalizes results into its own output format, so native QMD terminal hyperlinks are lost
  - `om status` / `om doctor` do not report QMD version, embedding health, pending vectors, or collection readiness
- `README.md`
  - still documents installing QMD from `github:tobi/qmd`
  - does not mention 2.1 features or any QMD-specific tuning path

### Practical implication

The current integration is good enough for "optional backend" support, but not yet good enough to make QMD 2.1 feel like a first-class `om` feature.

## What Is Actually Useful for `om`

### 1. `--no-rerank`

This is the most immediately useful 2.1 feature for `om`.

- `om context` and `om search` are latency-sensitive
- full QMD reranking is often the slowest part of `qmd query`
- hybrid retrieval without reranking is still higher recall than pure BM25 for many memory lookups

This gives `om` a better "fast enough for hooks" mode instead of the current hard split between:

- `bm25` = fast but lexical only
- `qmd-hybrid` = best quality but potentially slow/heavy

### 2. Per-collection model selection

This matters because `om`'s corpus is unusual:

- mostly short Markdown sections
- heavy use of exact tokens like `OM_SEARCH_BACKEND`, `mellona-hive`, `MEMORY.md`, `launchd`, and project slugs
- mostly English, but sometimes code/tool-heavy

QMD 2.1 lets us tune embed/rerank/generate models specifically for the `observational-memory` collection instead of assuming one global choice fits everything.

### 3. `qmd bench`

This gives us a clean way to stop guessing about search quality.

We can define a small set of realistic memory queries and expected hits, then compare:

- bundled BM25
- QMD BM25
- QMD hybrid with rerank
- QMD hybrid without rerank

That is especially useful before changing defaults.

### 4. JSON `line` output and editor links

These are useful, but only after `om` does extra work:

- `om search` currently hides native QMD CLI output, so clickable terminal links never reach the user
- QMD links point at generated `.qmd-docs/*.md` files, not original source files

This becomes more compelling if `om` starts surfacing source path/line metadata in its own output.

### 5. AST-aware chunking

This is a strong QMD feature, but it is **not** a priority for `om` right now because `om` indexes generated Markdown memory documents, not raw code files.

It only becomes relevant if `om` later expands into codebase-aware indexing.

## Recommended Direction

The right shape here is a staged adapter upgrade, not a search subsystem rewrite.

## First PR Scope

This implementation branch should cover:

- Phase 1: QMD config/env support plus dedicated index isolation
- Phase 2: `om status` / `om doctor` visibility
- README updates for the current QMD install path and hybrid setup
- targeted tests for config, backend command construction, and doctor/status reporting

This branch should explicitly defer:

- changing the default search backend
- benchmark fixtures and maintainer workflow glue
- raw QMD passthrough output
- source-path/line UX improvements beyond storing enough metadata to support them later
- any AST-aware chunking work

### Phase 1: Make QMD safe and configurable inside `om`

### Goal

Stop treating QMD as a black box subprocess and expose the minimal controls needed to benefit from 2.1.

### Changes

1. Add QMD-specific config fields in `src/observational_memory/config.py`

Recommended env vars:

- `OM_QMD_INDEX_NAME` defaulting to `observational-memory`
- `OM_QMD_NO_RERANK` defaulting to `0`
- `OM_QMD_EMBED_MODEL`
- `OM_QMD_RERANK_MODEL`
- `OM_QMD_GENERATE_MODEL`

2. Update `src/observational_memory/search/qmd.py`

- pass `--index <name>` on all QMD subprocess calls
- pass `--no-rerank` for hybrid query mode when configured
- pass QMD model env vars through the subprocess environment when configured
- keep collection creation/update isolated to `om`'s own index

### Why this first

Without a dedicated QMD index name, `om` shares QMD's default index and lifecycle with any other QMD usage on the machine. That makes automatic embedding, status checks, and tuning much harder to do safely.

### Phase 2: Improve operational visibility

### Goal

Make it obvious when QMD is installed, healthy, outdated, or missing embeddings.

### Changes

1. Extend `om status`

When `OM_SEARCH_BACKEND` is `qmd` or `qmd-hybrid`, show:

- whether `qmd` is installed
- which QMD index name `om` is using
- whether the `observational-memory` collection exists
- whether vectors are pending
- whether QMD looks like 2.1-capable

2. Extend `om doctor`

Add checks for:

- `qmd` binary present
- collection exists
- hybrid backend with no vectors embedded
- QMD installation path still on older GitHub install if relevant

### Why this matters

Right now it is easy for a user to set `OM_SEARCH_BACKEND=qmd-hybrid` and assume hybrid search is active even though embeddings may still be missing or QMD may not be on the expected version.

### Phase 3: Add a fast hybrid mode for hook-time retrieval

### Goal

Use QMD 2.1's `--no-rerank` to improve recall without paying the full reranking cost during startup and interactive memory lookups.

### Changes

1. Add a new backend name or search mode

Options:

- `qmd-hybrid-fast`
- or keep `qmd-hybrid` and gate reranking with `OM_QMD_NO_RERANK=1`

I recommend the env-var gate first because it is lower-friction and avoids backend proliferation.

2. Use the same mode for `om context` fallback retrieval

If the compact startup files are absent and `om` falls back to search-backed retrieval, it should be able to use QMD hybrid without incurring the slowest path.

### Expected outcome

Better recall than BM25 for session priming and memory lookup, with less latency than full reranking.

### Phase 4: Add maintainer benchmarking

### Goal

Turn search backend decisions into measured tradeoffs.

### Changes

1. Add a maintainer fixture, for example:

- `tests/fixtures/qmd-bench-memory.json`

2. Add a short maintainer workflow doc or Make target for:

- rebuilding the `om` QMD index
- running `qmd bench`
- comparing BM25 vs hybrid vs hybrid-no-rerank

### Query examples to include

- `"current project status"`
- `"launchd instead of cron"`
- `"green means green"`
- `"OM_CODEX_OBSERVER_INTERVAL_MINUTES"`
- `"agent visibility gap"`
- `"mellona hive branding"`

These queries cover both lexical and semantic retrieval patterns common in `om`.

### Phase 5: Improve search result UX

### Goal

Expose more of what QMD 2.1 now knows about each hit.

### Changes

1. Extend `SearchResult` / `Document.metadata` population in the QMD backend

Capture when available:

- `file`
- `line`
- `docid`

2. Extend `om search --json`

Include:

- source path
- line number
- raw QMD file/docid

3. Consider a `--raw-qmd` passthrough mode

This would let advanced users get QMD's native clickable terminal links directly.

### Important note

Native OSC 8 hyperlinks are only a real win if we either:

- preserve raw QMD output
- or map search hits back to meaningful source files instead of generated `.qmd-docs/*.md`

So this phase should follow the config and status work, not precede it.

## Docs Update

Update `README.md` to reflect the current QMD path:

- prefer `npm install -g @tobilu/qmd` or `bun install -g @tobilu/qmd`
- note that `om` benefits most from QMD `>= 2.1.0`
- explain the difference between:
  - `qmd`
  - `qmd-hybrid`
  - `qmd-hybrid` with `OM_QMD_NO_RERANK=1`
- explain that QMD hybrid requires embeddings to be built

## Explicit Non-Goals

- Do not add AST-aware chunking flags yet; they do not materially help the current Markdown-only `om` corpus.
- Do not auto-edit the user's global QMD config on first pass.
- Do not auto-run `qmd embed` against the shared default QMD index.
- Do not make QMD the default backend until benchmarking shows a clear win on real `om` queries.

## Recommended Implementation Order

1. Add QMD config/env support and dedicated index isolation
2. Add `--no-rerank` support in the backend
3. Add `status` / `doctor` visibility
4. Update README install and tuning docs
5. Add benchmark fixture and maintainer workflow
6. Revisit result UX and raw QMD passthrough

## Files Likely To Change

| File | Change |
|------|--------|
| `src/observational_memory/config.py` | New QMD config/env knobs |
| `src/observational_memory/search/qmd.py` | `--index`, `--no-rerank`, subprocess env, richer result metadata |
| `src/observational_memory/cli.py` | QMD status/doctor reporting, possibly search UX additions |
| `README.md` | QMD 2.1 install/tuning docs |
| `tests/test_search.py` | Backend config and result parsing coverage |
| `tests/test_cli_doctor.py` | QMD doctor checks |
| `tests/fixtures/` | Optional benchmark fixture |

## Verification

1. `uv run pytest tests/test_search.py tests/test_cli_doctor.py tests/test_config.py`
2. `OM_SEARCH_BACKEND=qmd om search --reindex "launchd"`
3. `OM_SEARCH_BACKEND=qmd-hybrid om search "current project status"`
4. `OM_SEARCH_BACKEND=qmd-hybrid OM_QMD_NO_RERANK=1 om search "current project status"`
5. `om status`
6. `om doctor --json`
7. Optional maintainer benchmark via `qmd bench <fixture.json>`

## Recommendation

Treat this as a **small-but-real integration upgrade**, not a rewrite.

The best near-term win is:

- dedicated QMD index isolation
- `--no-rerank` support
- model/env pass-through
- better status/doctor reporting

That gives `om` immediate value from QMD 2.1 without overcommitting to features that mostly matter for code search rather than observational memory.
