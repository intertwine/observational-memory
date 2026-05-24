# Maintainer Guide

This guide is for people changing, testing, and releasing Observational Memory. User install docs live in [install.md](install.md).

## Development Install

```bash
git clone https://github.com/intertwine/observational-memory.git
cd observational-memory
uv sync
uv pip install -e ".[dev]"
```

## Testing

```bash
# Using make (recommended)
make check          # lint + test
make test           # tests only
make lint           # linter only
make format         # auto-format
make qmd-bench      # repo-local QMD 2.1 benchmark fixture
make brew-formula   # generate Homebrew formula from current PyPI release
make brew-check     # sync into active tapped checkout and audit the Homebrew formula

# Or directly with uv. This matches CI.
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pytest tests/test_transcripts.py
uv run pytest -v
```

CI runs the lint step on Python 3.11, 3.12, and 3.13. A local `ruff check` pass is not enough; run `ruff format --check` too.

## Documentation Rules

Current docs should follow this layout:

- `README.md`: short user doorway, install snippet, architecture picture, and links.
- `docs/install.md`: user install and setup.
- `docs/integrations.md`: host-specific behavior.
- `docs/search-and-recall.md`: `om context`, `om recall`, `om search`, and QMD basics.
- `docs/configuration.md`: env vars, paths, schedules, providers, and search backends.
- `docs/om-cluster-sync.md`: cluster operations and security model.
- `docs/om-cluster-validation.md`: public-safe cluster validation.
- `docs/MAINTAINERS.md`: maintainer and release workflows.

Use plain English. Aim for a 10th grade reading level. Prefer short sections, tables, and working CLI snippets. Archive completed implementation plans under `docs/archive/` instead of linking them as current guidance.

## QMD Benchmarking

QMD 2.1 adds `qmd bench`, and this repo now ships a small OM-shaped benchmark corpus so maintainers can compare retrieval modes without depending on a live `~/.local/share/observational-memory` store.

```bash
# Rebuild the dedicated benchmark collection from the repo fixture corpus
make qmd-bench-setup

# Build embeddings for vector / hybrid evaluation
make qmd-bench-embed

# Run the benchmark fixture with human-readable output
make qmd-bench

# Capture machine-readable results
make qmd-bench-json > /tmp/om-qmd-bench.json

# Fail fast if the local qmd install is too old for bench support
make qmd-bench-preflight
```

Default benchmark settings:

- index: `om-bench`
- collection: `om-bench-memory`
- corpus: `tests/fixtures/qmd-bench-corpus/`
- fixture: `tests/fixtures/qmd-bench-memory.json`

Override them if you need to isolate a run:

```bash
make qmd-bench QMD_BENCH_INDEX=om-bench-alt
```

Important maintainer rules:

- Keep the corpus repo-local and reviewable; do not point the fixture at a personal OM memory directory.
- Keep `expected_files` paths in the fixture relative to the corpus root so they match QMD's benchmark expectations.
- If you change the corpus or fixture, keep `tests/test_qmd_bench_fixture.py` passing in the same PR so fixture drift stays visible in CI.

## QMD Release Validation

If a release changes QMD-related behavior, run a quick user-facing validation pass in addition to the fixture benchmark:

```bash
om status
om doctor
OM_SEARCH_BACKEND=qmd om search --reindex "launchd"
OM_SEARCH_BACKEND=qmd-hybrid om search "current project status"
OM_SEARCH_BACKEND=qmd-hybrid OM_QMD_NO_RERANK=1 om search "current project status"
OM_SEARCH_BACKEND=qmd-hybrid om search "launchd" --json
OM_SEARCH_BACKEND=qmd-hybrid om search "launchd" --raw-qmd
```

Confirm:

- `om status` and `om doctor` agree about install health, collection readiness, and embedding state.
- The first `qmd embed` pass may download QMD's local embedding model; treat that as expected first-run setup, not an OM regression.
- `--raw-qmd` preserves native QMD output without OM reindex banners mixed into stdout.
- `--json` exposes `source_path`, `source_line`, `qmd_file`, `qmd_docid`, and `qmd_line` when available.
- `OM_QMD_NO_RERANK=1` is only reported as active when the installed QMD actually supports it, and the no-rerank path should stay on the fast typed lex+vec query flow.

## Post-Merge Machine Green

After upgrading, reinstalling, or pulling a release fix, use this lane to confirm the local box is healthy before returning to normal work:

```bash
om status
qmd --index observational-memory status
qmd --index observational-memory embed
om doctor --validate-key
om status
```

What to look for:

- `om status` should show the resolved observer and reflector models, the active search backend, and whether OM sees launchd or cron as the background scheduler.
- `qmd --index observational-memory status` should show the collection, embedded vectors, and any pending vectors.
- `qmd --index observational-memory embed` is the repair step when the index exists but embeddings are missing or stale.
- `om doctor --validate-key` should pass with zero warnings or failures and confirm the configured provider can make a live call.
- On macOS, `om doctor` should report LaunchAgents as loaded, with no duplicate cron backstop left behind.

If you changed local Codex or agent skill files as part of the same upgrade, run the relevant skill validator too before declaring the machine green.

## Codex Integration Model

Codex is now hooks-first, not AGENTS-first.

Installer-managed user-level files:

- `~/.codex/config.toml`
  `om install --codex` ensures `[features].hooks = true` and, for older Codex CLI compatibility during the rename, `[features].codex_hooks = true`.
- `~/.codex/hooks.json`
  OM installs and removes only its own global `SessionStart` and `Stop` hook groups.
- `~/.codex/AGENTS.md`
  OM keeps a conditional fallback block here for hook-disabled or older Codex setups.

Runtime expectations:

- `SessionStart` runs `om context` to inject a budgeted startup pack.
- `Stop` queues transcript-specific Codex checkpointing through the hidden `om codex-checkpoint` path.
- a background scheduler remains installed as a backstop for Codex transcript observation:
  - `launchd` on macOS by default
  - cron on other Unix-like platforms by default
- agents can expand omitted startup sections with `om recall --handle ...`
- agents can retrieve deeper context with `om recall --query ...`
- `om status` and `om doctor` should report launchd vs cron truthfully, including duplicate macOS backstops.

Important maintainer rules:

- Preserve unrelated user or third-party hook groups in `hooks.json`.
- Do not default to repo-local `.codex/hooks.json`; OM is intentionally user-level shared memory.
- `om uninstall --codex` should remove OM-managed hooks and the OM AGENTS fallback block, but should not disable `hooks = true` or `codex_hooks = true`.
- AGENTS should stay conditional fallback only. Avoid reintroducing unconditional startup reads when hooks are present.

## Hermes Integration Model

Hermes support has two layers:

- core `om` transcript ingestion for Hermes session logs;
- the external `intertwine/hermes-observational-memory` Hermes memory-provider plugin for live startup context, search, explicit writes, and optional OM Cluster participation.

Runtime expectations:

- `om observe --source hermes` scans recent Hermes session logs in `~/.hermes/sessions/`.
- `om observe --transcript /path/to/session.jsonl --source hermes` processes one Hermes session explicitly.
- The Hermes parser keeps user messages, assistant prose, and summarized tool calls.
- It intentionally drops `session_meta`, raw tool output, and other machine-oriented records before the observer LLM sees them.
- `om install` does not manage Hermes hooks, install the Hermes plugin, or set `memory.provider`; keep docs and status output truthful about that scope.
- The Hermes plugin is installed with `hermes plugins install intertwine/hermes-observational-memory --no-enable` and activated with `hermes memory setup`.
- Keep the plugin dependency line aligned with the current OM release line; for `v0.6.5`, the plugin should require `observational-memory>=0.6.5,<0.7`.

Tests that should protect Hermes behavior:

- `tests/test_transcripts.py`
- `tests/test_cli_observe.py`

The standalone plugin has its own tests in the `intertwine/hermes-observational-memory` repo. Do not treat this repo's Hermes parser tests as proof that the live Hermes plugin still loads under current Hermes.

## OpenAI Batch Live Smoke (opt-in)

The `om reflect --async` Batch path is fully covered by mocked tests
(`tests/test_jobs_batch.py`). A live end-to-end smoke needs a real `OPENAI_API_KEY`
with usable billing (the v0.6.5 validation hit `billing_hard_limit_reached`, which
blocked a real run). When billing is available, run a tiny end-to-end check:

```bash
export OPENAI_API_KEY=sk-...           # billing enabled
export OM_LLM_REFLECTOR_PROVIDER=openai
export OM_OPENAI_MODEL=gpt-4o-mini     # cheap model for the smoke

om reflect --async                     # submit; prints job id + batch id
om jobs list                           # status: submitted
# wait for the batch to complete (up to 24h; usually minutes for one request)
om jobs poll                           # applies when completed; reflections.md updates
om jobs show <job_id>                  # status: applied
```

Confirm: the job reaches `applied`, `reflections.md` updates, `om usage tail` shows
the recorded call at the Batch (half) price, and the uploaded input/output files are
gone from your OpenAI account. The `openai-chatgpt` subscription provider has no
Batch API — `OM_LLM_REFLECTOR_PROVIDER=openai-chatgpt om reflect --async` must error
clearly (also asserted in tests).

## Grok Integration Model

Grok Build TUI is a first-class local integration.

Installer-managed user-level files:

- `~/.grok/hooks/observational-memory.json`
  OM installs and removes only its own Grok hook file.
- `~/.claude/settings.json`
  Grok may also read Claude-compatible hooks. `om install --grok` checks this file and skips duplicate native `SessionStart` context injection when OM Claude hooks already exist.

Runtime expectations:

- `SessionStart` runs `om context` through native Grok hooks when Claude compatibility is not already providing OM context.
- `SessionEnd`, `UserPromptSubmit`, and `PreCompact` call the hidden `om grok-checkpoint` path.
- `om observe --source grok` scans recent `~/.grok/sessions/<cwd>/<session-id>/updates.jsonl` files.
- Grok cursors are count-based because streaming chunks can share timestamps.
- Grok native memory is independent of OM memory. Do not present one as replacing the other.

Tests that should protect Grok behavior:

- `tests/test_transcripts.py::TestGrokParser`
- `tests/test_observe.py::TestGrokObserver`
- `tests/test_cli_install.py::TestGrokInstall`

## Homebrew Release

`observational-memory` is published to Homebrew via a tap formula (to avoid name collisions with short/common formula names). The executable remains `om`.
The Homebrew release workflow also checks Homebrew/core to catch formula-name collisions before pushing tap updates.

### One-time setup

1. Create tap repo: `intertwine/homebrew-tap`
2. Add repo variable in this repo: `HOMEBREW_TAP_REPO=intertwine/homebrew-tap`
3. Add repo secret in this repo: `HOMEBREW_TAP_GITHUB_TOKEN` (token with push access to the tap repo)

### Per-release flow

1. Publish new version to PyPI.
2. Tag the same version in git (for example `vX.Y.Z`) and push the tag.
3. GitHub Actions workflow `.github/workflows/homebrew-release.yml` regenerates `packaging/homebrew/observational-memory.rb` from PyPI, updates `Formula/observational-memory.rb` in the tap repo, then commits and pushes the tap update.

### Local maintainership commands

```bash
# Regenerate formula locally
make brew-formula
# Audit against the active tapped checkout (requires `brew tap intertwine/tap`)
make brew-check

# Copy into a local tap checkout
make release-homebrew HOMEBREW_TAP_DIR=../homebrew-tap
```

`make brew-check` does two things:

1. Regenerates `packaging/homebrew/observational-memory.rb`
2. Copies that generated file into the active tapped checkout returned by `brew --repository intertwine/tap`, then runs `brew audit --strict --formula intertwine/tap/observational-memory`

This means `make brew-check` validates the same formula path that Homebrew actually audits, instead of only checking the generated file in this repo.
If `intertwine/tap` is not tapped locally, `make brew-check` exits with instructions instead of reporting a misleading success.

## Current Release Process (v0.6.4 example)

`v0.6.4` is the current release (a stability patch raising the `SessionStart` timeout to 15 s and adding the permanent `make verify-session-start` test). The release process below is the one used for 0.6.4 and should be followed for future releases.

Before cutting a patch release:

```bash
git status --short
make check
make verify-session-start
uv run ruff check .
uv run ruff format --check .
uv run pytest
OM_CLUSTER_ENABLED=0 uv run om context >/tmp/om-context.json
uv run om recall --query "current work" --limit 3
```

Recommended cluster checks:

```bash
uv run pytest tests/sync/test_filesystem_sync.py tests/sync/test_relay_transport.py
uv run pytest tests/sync/test_store_and_materialize.py
```

### SessionStart hook regression test

After any change that touches hook registration (`_install_*`), the `context` command, `startup_memory.py`, or the hook shell scripts in `src/observational_memory/hooks/`, run:

```bash
make verify-session-start
# or directly
uv run python scripts/verify_session_start_hooks.py --keep   # for post-mortem inspection
```

The script:
- Performs a completely isolated `om install --all` using the current source tree.
- Asserts that **all** `SessionStart` registrations (Claude, Codex, and Grok via inheritance) use the safe timeout (currently 15 s).
- Executes the exact command strings the host agents will run at session start.
- Validates that they emit correct `hookSpecificOutput` JSON containing the budgeted OM startup context.

It is the authoritative way to prove "the om session start issue is fixed and will stay fixed."

Release flow:

1. Confirm the docs and release notes in the latest [RELEASE-*.md](RELEASE-0.6.4.md) file (or create a new one for the next version).
2. Bump the version with the appropriate `make bump-version BUMP=...` command.
3. Run `make check`.
4. Build with `make build`.
5. Publish to PyPI.
6. Push the tag.
7. Watch the Homebrew release workflow.

## File Structure

```text
observational-memory/
├── README.md                         # Short user doorway
├── docs/MAINTAINERS.md               # This file
├── docs/install.md                   # User install guide
├── docs/integrations.md              # Agent/platform integrations
├── docs/search-and-recall.md         # Startup, recall, search, QMD basics
├── docs/configuration.md             # Env vars, paths, providers, schedules
├── docs/om-cluster-sync.md           # Cluster operations
├── docs/om-cluster-validation.md     # Public-safe validation checklist
├── docs/archive/                     # Old plans and status reports
├── LICENSE                           # MIT
├── pyproject.toml                    # Python package config
├── src/observational_memory/
│   ├── cli.py                        # CLI: observe, reflect, recall, search, cluster, install, status
│   ├── config.py                     # Paths, defaults, env detection
│   ├── llm.py                        # LLM API abstraction (direct + enterprise providers)
│   ├── observe.py                    # Observer logic
│   ├── reflect.py                    # Reflector logic
│   ├── startup_memory.py             # Budgeted startup packs and recall handles
│   ├── reflection_metadata.py        # Inline metadata, local scope, conflict detection
│   ├── sync/                         # OM Cluster records, transports, relay, materialization
│   ├── transcripts/
│   │   ├── claude.py                 # Claude Code JSONL parser
│   │   ├── codex.py                  # Codex CLI session parser
│   │   ├── grok.py                   # Grok Build TUI updates.jsonl parser
│   │   ├── hermes.py                 # Hermes Agent session parser
│   │   └── auto_memory.py            # Claude Code auto-memory scanner
│   ├── search/                       # Pluggable search over memory files
│   │   ├── __init__.py               # Document model, factory, reindex orchestrator
│   │   ├── backend.py                # SearchBackend Protocol
│   │   ├── parser.py                 # Parse observations/reflections/auto-memory into Documents
│   │   ├── bm25.py                   # BM25 backend (default, uses rank-bm25)
│   │   ├── qmd.py                    # QMD backend (optional, shells out to qmd CLI)
│   │   └── none.py                   # No-op backend
│   ├── prompts/
│   │   ├── observer.md               # Observer system prompt
│   │   └── reflector.md              # Reflector system prompt
│   └── hooks/
│       ├── claude/                   # Claude startup and checkpoint hooks
│       └── grok/                     # Grok startup hook
└── tests/
    ├── test_cli_context.py           # Context injection tests
    ├── test_cli_observe.py           # Observe CLI routing tests
    ├── test_cli_search.py            # Search CLI output tests
    ├── test_cli_version.py           # Root CLI flag tests
    ├── test_qmd_bench_fixture.py     # QMD benchmark fixture integrity checks
    ├── test_transcripts.py           # Transcript parser tests
    ├── test_observe.py               # Observer tests
    ├── test_reflect.py               # Reflector tests
    ├── test_search.py                # Search module tests
    ├── test_auto_memory.py           # Auto-memory scanner tests
    └── fixtures/                     # Sample transcripts + QMD benchmark corpus
```
