# Maintainer Guide

This document contains developer and maintainer workflows that were moved out of the main README to keep onboarding focused.

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

# Or directly with uv
uv sync
uv run pytest
uv run pytest tests/test_transcripts.py
uv run pytest -v
```

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

- `SessionStart` runs `om context` to inject `profile.md` + `active.md`.
- `Stop` queues transcript-specific Codex checkpointing through the hidden `om codex-checkpoint` path.
- a background scheduler remains installed as a backstop for Codex transcript observation:
  - `launchd` on macOS by default
  - cron on other Unix-like platforms by default
- `om status` and `om doctor` should report launchd vs cron truthfully, including duplicate macOS backstops.

Important maintainer rules:

- Preserve unrelated user or third-party hook groups in `hooks.json`.
- Do not default to repo-local `.codex/hooks.json`; OM is intentionally user-level shared memory.
- `om uninstall --codex` should remove OM-managed hooks and the OM AGENTS fallback block, but should not disable `hooks = true` or `codex_hooks = true`.
- AGENTS should stay conditional fallback only; avoid reintroducing unconditional startup reads when hooks are present.

## Hermes Integration Model

Hermes support is currently transcript ingestion support, not a hook installer.

Runtime expectations:

- `om observe --source hermes` scans recent Hermes session logs in `~/.hermes/sessions/`.
- `om observe --transcript /path/to/session.jsonl --source hermes` processes one Hermes session explicitly.
- The Hermes parser keeps user messages, assistant prose, and summarized tool calls.
- It intentionally drops `session_meta`, raw tool output, and other machine-oriented records before the observer LLM sees them.
- `om install` does not currently manage Hermes hooks or a Hermes-specific scheduler backstop; keep docs and status output truthful about that scope.

Tests that should protect Hermes behavior:

- `tests/test_transcripts.py`
- `tests/test_cli_observe.py`

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

## File Structure

```text
observational-memory/
├── README.md                         # User-facing docs
├── docs/MAINTAINERS.md               # This file
├── LICENSE                           # MIT
├── pyproject.toml                    # Python package config
├── src/observational_memory/
│   ├── cli.py                        # CLI: om observe, reflect, search, backfill, install, status
│   ├── config.py                     # Paths, defaults, env detection
│   ├── llm.py                        # LLM API abstraction (direct + enterprise providers)
│   ├── observe.py                    # Observer logic
│   ├── reflect.py                    # Reflector logic
│   ├── startup_memory.py             # Compact startup profile/active file generation
│   ├── transcripts/
│   │   ├── claude.py                 # Claude Code JSONL parser
│   │   ├── codex.py                  # Codex CLI session parser
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
│   └── hooks/claude/
│       ├── session-start.sh          # Inject memory on session start (search-backed)
│       └── session-end.sh            # Trigger observer on session end
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
