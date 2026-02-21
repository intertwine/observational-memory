# Makefile for observational-memory
# Cross-agent shared memory for Claude Code and Codex CLI

SHELL := /bin/bash

.PHONY: help test lint format check build clean bump-version publish-test publish install-dev doctor brew-formula brew-check release-homebrew brew-install

# ---------- Colors (portable) ----------
ifdef NO_COLOR
RED :=
GREEN :=
YELLOW :=
NC :=
else
ESC := $(shell printf '\033')
RED := $(ESC)[0;31m
GREEN := $(ESC)[0;32m
YELLOW := $(ESC)[1;33m
NC := $(ESC)[0m
endif
ECHO = printf "%b\n"

# Version bump type (patch, minor, major)
BUMP ?= patch
HOMEBREW_TAP_DIR ?= ../homebrew-tap
HOMEBREW_INSTALL_TARGET ?= intertwine/tap/observational-memory

help:
	@$(ECHO) "$(GREEN)Observational Memory - Development Commands$(NC)"
	@$(ECHO) ""
	@$(ECHO) "$(YELLOW)Quality:$(NC)"
	@$(ECHO) "  make test           - Run all tests"
	@$(ECHO) "  make lint           - Run linter checks"
	@$(ECHO) "  make format         - Auto-format code"
	@$(ECHO) "  make check          - Run all quality checks (lint + format check + test)"
	@$(ECHO) ""
	@$(ECHO) "$(YELLOW)Building:$(NC)"
	@$(ECHO) "  make build          - Build sdist and wheel"
	@$(ECHO) "  make clean          - Remove build artifacts"
	@$(ECHO) ""
	@$(ECHO) "$(YELLOW)Versioning:$(NC)"
	@$(ECHO) "  make bump-version              - Bump patch version (default)"
	@$(ECHO) "  make bump-version BUMP=minor   - Bump minor version"
	@$(ECHO) "  make bump-version BUMP=major   - Bump major version"
	@$(ECHO) ""
	@$(ECHO) "$(YELLOW)Publishing:$(NC)"
	@$(ECHO) "  make publish-test   - Publish to TestPyPI"
	@$(ECHO) "  make publish        - Publish to PyPI (production)"
	@$(ECHO) "  make brew-formula   - Generate Homebrew formula at packaging/homebrew/observational-memory.rb"
	@$(ECHO) "  make brew-check     - Run Homebrew formula audit locally (if brew is installed)"
	@$(ECHO) "  make release-homebrew - Copy formula into local tap checkout (HOMEBREW_TAP_DIR)"
	@$(ECHO) "  make brew-install   - Install from Homebrew tap target ($(HOMEBREW_INSTALL_TARGET))"
	@$(ECHO) ""
	@$(ECHO) "$(YELLOW)Development:$(NC)"
	@$(ECHO) "  make install-dev    - Install in editable mode with dev deps"
	@$(ECHO) "  make doctor         - Run om doctor diagnostics"

# Run all tests
test:
	@$(ECHO) "$(YELLOW)Running tests...$(NC)"
	@uv run pytest -q
	@$(ECHO) "$(GREEN)✓ Tests passed$(NC)"

# Run linter checks
lint:
	@$(ECHO) "$(YELLOW)Running linter...$(NC)"
	@uv run ruff check .
	@uv run ruff format --check .
	@$(ECHO) "$(GREEN)✓ Lint passed$(NC)"

# Auto-format code
format:
	@$(ECHO) "$(YELLOW)Formatting code...$(NC)"
	@uv run ruff check . --fix
	@uv run ruff format .
	@$(ECHO) "$(GREEN)✓ Code formatted$(NC)"

# Run all quality checks
check: lint test
	@$(ECHO) "$(GREEN)✓ All checks passed$(NC)"

# Build sdist and wheel
build: clean
	@$(ECHO) "$(YELLOW)Building package...$(NC)"
	@uv run python -m build
	@$(ECHO) "$(GREEN)✓ Package built$(NC)"
	@ls -lh dist/

# Clean build artifacts
clean:
	@$(ECHO) "$(YELLOW)Cleaning build artifacts...$(NC)"
	@rm -rf dist/ build/ src/*.egg-info
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@$(ECHO) "$(GREEN)✓ Clean$(NC)"

# Bump version
bump-version:
	@if [ "$(BUMP)" != "patch" ] && [ "$(BUMP)" != "minor" ] && [ "$(BUMP)" != "major" ]; then \
		$(ECHO) "$(RED)Error: BUMP must be 'patch', 'minor', or 'major'$(NC)"; \
		exit 1; \
	fi
	@$(ECHO) "$(YELLOW)Bumping $(BUMP) version...$(NC)"
	@VERSION_CHANGE=$$(uv run python scripts/bump_version.py pyproject.toml $(BUMP)); \
	$(ECHO) "$(GREEN)✓ Version updated: $$VERSION_CHANGE$(NC)"

# Publish to TestPyPI
publish-test: build
	@$(ECHO) "$(YELLOW)Publishing to TestPyPI...$(NC)"
	@uv run twine upload --repository testpypi dist/*
	@$(ECHO) "$(GREEN)✓ Published to TestPyPI$(NC)"
	@$(ECHO) "$(YELLOW)Install with: uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ observational-memory$(NC)"

# Publish to PyPI (production)
publish: build
	@$(ECHO) "$(RED)⚠️  Publishing to production PyPI!$(NC)"
	@$(ECHO) -n "Continue? [y/N] " && read ans && ( [ "$${ans}" = "y" ] || [ "$${ans}" = "Y" ] )
	@uv run twine upload dist/*
	@$(ECHO) "$(GREEN)✓ Published to PyPI$(NC)"

# Install in editable mode with dev dependencies
install-dev:
	@$(ECHO) "$(YELLOW)Installing in editable mode...$(NC)"
	@uv sync
	@uv pip install -e ".[dev]"
	@$(ECHO) "$(GREEN)✓ Installed$(NC)"

# Run diagnostics
doctor:
	@uv run om doctor

# Generate Homebrew formula from PyPI artifacts (root package + transitive resources)
brew-formula:
	@$(ECHO) "$(YELLOW)Generating Homebrew formula...$(NC)"
	@uv run --with pip python scripts/generate_homebrew_formula.py --output packaging/homebrew/observational-memory.rb
	@$(ECHO) "$(GREEN)✓ Homebrew formula generated$(NC)"

# Audit Homebrew formula locally
brew-check: brew-formula
	@$(ECHO) "$(YELLOW)Auditing Homebrew formula...$(NC)"
	@if ! command -v brew >/dev/null 2>&1; then \
		$(ECHO) "$(RED)Error: brew not found$(NC)"; \
		$(ECHO) "Install Homebrew first: https://brew.sh"; \
		exit 1; \
	fi
	@brew style packaging/homebrew/observational-memory.rb
	@if brew info --formula $(HOMEBREW_INSTALL_TARGET) >/dev/null 2>&1; then \
		brew audit --strict --formula $(HOMEBREW_INSTALL_TARGET); \
	else \
		$(ECHO) "$(YELLOW)Skipped brew audit by formula name; add tap first: brew tap intertwine/tap$(NC)"; \
	fi
	@$(ECHO) "$(GREEN)✓ Homebrew formula audit passed$(NC)"

# Sync formula into a local tap checkout for publishing
release-homebrew: brew-formula
	@$(ECHO) "$(YELLOW)Syncing formula into tap checkout: $(HOMEBREW_TAP_DIR)$(NC)"
	@if [ ! -d "$(HOMEBREW_TAP_DIR)/.git" ]; then \
		$(ECHO) "$(RED)Error: $(HOMEBREW_TAP_DIR) is not a git repository$(NC)"; \
		$(ECHO) "Clone your tap repo first, e.g.:"; \
		$(ECHO) "  git clone git@github.com:intertwine/homebrew-tap.git $(HOMEBREW_TAP_DIR)"; \
		exit 1; \
	fi
	@mkdir -p "$(HOMEBREW_TAP_DIR)/Formula"
	@cp packaging/homebrew/observational-memory.rb "$(HOMEBREW_TAP_DIR)/Formula/observational-memory.rb"
	@$(ECHO) "$(GREEN)✓ Synced $(HOMEBREW_TAP_DIR)/Formula/observational-memory.rb$(NC)"
	@$(ECHO) "Next: cd $(HOMEBREW_TAP_DIR) && git add Formula/observational-memory.rb && git commit -m 'Update observational-memory formula' && git push"

# Install from Homebrew tap target
brew-install:
	@$(ECHO) "$(YELLOW)Installing via Homebrew target: $(HOMEBREW_INSTALL_TARGET)$(NC)"
	@if ! command -v brew >/dev/null 2>&1; then \
		$(ECHO) "$(RED)Error: brew not found$(NC)"; \
		$(ECHO) "Install Homebrew first: https://brew.sh"; \
		exit 1; \
	fi
	@brew install $(HOMEBREW_INSTALL_TARGET)
