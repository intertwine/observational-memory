# Makefile for observational-memory
# Cross-agent shared memory for Claude Code and Codex CLI

SHELL := /bin/bash

.PHONY: help test lint format check build clean bump-version publish-test publish install-dev doctor

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
