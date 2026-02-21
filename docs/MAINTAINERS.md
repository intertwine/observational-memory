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
make brew-formula   # generate Homebrew formula from current PyPI release
make brew-check     # audit Homebrew formula (requires brew)

# Or directly with uv
uv sync
uv run pytest
uv run pytest tests/test_transcripts.py
uv run pytest -v
```

## Homebrew Release

`observational-memory` is published to Homebrew via a tap formula (to avoid name collisions with short/common formula names). The executable remains `om`.
The Homebrew release workflow also checks Homebrew/core to catch formula-name collisions before pushing tap updates.

### One-time setup

1. Create tap repo: `intertwine/homebrew-tap`
2. Add repo variable in this repo: `HOMEBREW_TAP_REPO=intertwine/homebrew-tap`
3. Add repo secret in this repo: `HOMEBREW_TAP_GITHUB_TOKEN` (token with push access to the tap repo)

### Per-release flow

1. Publish new version to PyPI.
2. Tag the same version in git (for example `v0.1.2`) and push the tag.
3. GitHub Actions workflow `.github/workflows/homebrew-release.yml` regenerates `packaging/homebrew/observational-memory.rb` from PyPI, updates `Formula/observational-memory.rb` in the tap repo, then commits and pushes the tap update.

### Local maintainership commands

```bash
# Regenerate formula locally
make brew-formula

# Copy into a local tap checkout
make release-homebrew HOMEBREW_TAP_DIR=../homebrew-tap
```

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
│   ├── transcripts/
│   │   ├── claude.py                 # Claude Code JSONL parser
│   │   └── codex.py                  # Codex CLI session parser
│   ├── search/                       # Pluggable search over memory files
│   │   ├── __init__.py               # Document model, factory, reindex orchestrator
│   │   ├── backend.py                # SearchBackend Protocol
│   │   ├── parser.py                 # Parse observations/reflections into Documents
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
    ├── test_transcripts.py           # Transcript parser tests
    ├── test_observe.py               # Observer tests
    ├── test_reflect.py               # Reflector tests
    ├── test_search.py                # Search module tests
    └── fixtures/                     # Sample transcripts
```
