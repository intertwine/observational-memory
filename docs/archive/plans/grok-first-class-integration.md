# First-Class Grok Support Plan

**Status:** Completed in PR #49.

## Goal

Add first-class support for the Grok Build TUI as a peer integration alongside Claude Code, Codex, Cowork, and Hermes.

Users should be able to:

- Run `om install --grok` or `om install --all` and get startup context.
- Have Grok sessions contribute observations through `om observe --source grok`.
- Use the existing OM surfaces from Grok: `om context`, `om recall`, and `om search`.
- Avoid duplicate startup context when Grok also reads existing Claude Code hooks.

## Key Decisions

- `om install --grok` creates an OM-owned `~/.grok/hooks/observational-memory.json`.
- The installer checks `~/.claude/settings.json` for an existing OM `SessionStart` hook.
- If Claude OM startup context is already present, the Grok hook file omits native `SessionStart` and only registers checkpoint events.
- If Claude OM startup context is absent, the Grok hook file registers native `SessionStart` through `src/observational_memory/hooks/grok/session-start.sh`.
- Grok checkpoint hooks call the hidden `om grok-checkpoint` command for `SessionEnd`, `UserPromptSubmit`, and `PreCompact`.
- Windows hook commands mirror the Claude strategy by invoking `om` directly instead of shell scripts.
- Grok observation uses a dedicated `transcripts/grok.py` parser for `updates.jsonl`.
- Cursoring is count-based to avoid reprocessing same-timestamp streaming chunks.
- Grok native memory is documented as an independent peer, not a replacement for OM.

## Delivered Surface

- `om install --grok`
- `om uninstall --grok`
- `om observe --source grok`
- `om grok-checkpoint --transcript <updates.jsonl>`
- `om status` Grok reporting
- `om doctor` Grok reporting
- `Config.grok_home`, `grok_hooks_dir`, `grok_sessions_dir`, and `grok_config_path`
- Grok section in `docs/integrations.md`
- Installed Codex `AGENTS.md` fallback text that mentions Grok support

## Validation Targets

Reviewers should check:

```bash
uv run om install --grok --non-interactive
uv run om doctor
uv run om observe --source grok --dry-run
uv run om grok-checkpoint --transcript ~/.grok/sessions/.../updates.jsonl
uv run pytest tests/test_transcripts.py::TestGrokParser tests/test_cli_install.py::TestGrokInstall
```

The full repository validation remains:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Follow-Up Ideas

- Consider packaging OM as a Grok plugin if Grok's plugin path stabilizes.
- Consider watching common Grok project memory files in a future auto-memory pass.
- Consider richer parsing of `summary.json` and terminal logs once the `updates.jsonl` path has real-world mileage.
