# Plan: Hooks-First Codex Integration for Observational Memory

## Context

Today the Codex path in `om` is intentionally simple:

- startup context is injected indirectly via `~/.codex/AGENTS.md`
- Codex observations are captured by a polling cron job (`om observe --source codex`)
- there is no Codex-native hook installation path yet

That was the right tradeoff before Codex exposed hooks, but it is now the weaker integration surface.

The new official Codex hooks docs change the picture:

- hooks are now available behind `[features].codex_hooks = true` in `~/.codex/config.toml`
- Codex discovers hooks from `~/.codex/hooks.json` and `<repo>/.codex/hooks.json`
- `SessionStart` can inject extra developer context directly
- `Stop` runs at turn scope and is the closest Codex analogue to Claude's end-of-turn/session checkpoint opportunities
- `AGENTS.md` is still useful, but it is loaded as part of the instruction chain at session start rather than acting like an event-driven integration point

That makes a hooks-first Codex path the right long-term direction for `om`.

## Ground Truth From This Repo

Current `om` capabilities already line up well with a hooks-based design:

- `src/observational_memory/cli.py`
  - `_install_codex()` currently writes the OM block into `~/.codex/AGENTS.md`
  - `_install_cron()` currently installs the Codex polling observer
  - hidden `om context` already emits the exact `SessionStart` JSON shape Codex expects (`hookSpecificOutput.additionalContext`)
- `src/observational_memory/startup_memory.py`
  - compact startup files (`profile.md` + `active.md`) already exist and are regenerated from reflections/observations
- `src/observational_memory/observe.py`
  - Codex observation is currently "scan all recent sessions"
- `src/observational_memory/transcripts/codex.py`
  - parser already supports both JSON and JSONL Codex transcript formats

So this is not a greenfield project. The core memory model is already in place. What is missing is a Codex-native installation/runtime layer.

## Goals

1. Make Codex hooks the primary integration path.
2. Keep startup context graceful when hooks are unavailable, disabled, or broken.
3. Keep observation capture graceful when hooks are unavailable or a turn never reaches `Stop`.
4. Avoid duplicating startup context when both hooks and `AGENTS.md` are present.
5. Preserve user-owned Codex config and third-party hooks.

## Non-Goals

- No attempt to replace repo-local or team-local hooks. OM should coexist with them.
- No `PreToolUse` / `PostToolUse` behavior in the first pass. They are not needed for observational memory.
- No uninstall behavior that tries to turn off `codex_hooks` globally. If the user or another tool wants hooks enabled, OM should not fight that.

## Recommended Target Architecture

### 1. Hooks become the primary startup path

Install a global Codex hook in `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "\"/absolute/path/to/om\" context",
            "statusMessage": "Loading observational memory..."
          }
        ]
      }
    ]
  }
}
```

Rationale:

- `om context` already outputs the exact `SessionStart` JSON Codex expects
- `startup|resume` matches both fresh launches and resumed sessions
- this mirrors the Claude `SessionStart` hook model closely

### 2. Hooks become the preferred checkpoint path

Add an OM-managed `Stop` hook that triggers an incremental Codex observation checkpoint for the active transcript.

Recommended first-pass behavior:

- `Stop` hook runs a new OM checkpoint entrypoint
- the checkpoint entrypoint reads hook JSON from `stdin`
- if `transcript_path` is present, it observes only that transcript
- if `transcript_path` is missing, it exits quietly and cron remains the backstop
- work runs in the background so the stop hook does not add noticeable turn latency
- use a per-transcript lock so multiple turns cannot spawn duplicate observers
- skip if nothing new has been written since the last successful checkpoint

Why `Stop` and not `UserPromptSubmit` as the primary checkpoint event:

- `Stop` is after the turn is complete, which is the closest equivalent to Claude's `SessionEnd`
- it naturally captures completed user/assistant exchanges
- it avoids observing too early, before the latest assistant turn is written

`UserPromptSubmit` can stay out of the first implementation. We can add it later if we find `Stop` leaves too much uncovered mid-session.

### 3. AGENTS.md becomes a conditional fallback, not the primary path

Do not remove the AGENTS integration entirely. Rewrite it so it becomes a deduplicating fallback:

- if startup context is already present in developer context, do nothing
- otherwise, read `profile.md` and `active.md`
- deeper context still remains available through `om search`, `reflections.md`, and `observations.md`

Concretely, the new global OM block in `~/.codex/AGENTS.md` should say something like:

> OM usually injects startup context through Codex hooks. If this session does not already include sections titled `# Startup Profile` and `# Active Context`, read `profile.md` and `active.md` before substantial work.

This gives us graceful fallback on older Codex builds or on machines where hooks are disabled, while reducing the chance of duplicate file reads when hooks are working.

### 4. Cron remains the safety net

Keep the existing Codex polling cron job even after hooks ship.

Rationale:

- hooks are explicitly experimental
- a session can die mid-turn and never reach `Stop`
- cron is already implemented and low-risk
- when hooks are working, cron should mostly no-op because the cursor is already current

This is the clean fallback story:

- startup fallback: `AGENTS.md`
- checkpoint fallback: cron observer

## Implementation Plan

### Step 1: Extend config paths and status surface

**File:** `src/observational_memory/config.py`

Add:

- `codex_config_path -> ~/.codex/config.toml`
- `codex_hooks_path -> ~/.codex/hooks.json`

These paths should sit next to the existing `codex_home` / `codex_agents_md` helpers.

### Step 2: Add a safe `config.toml` upsert for `codex_hooks = true`

**File:** `src/observational_memory/cli.py`

Add a helper that:

- creates `~/.codex/config.toml` if missing
- ensures `[features]` exists
- ensures `codex_hooks = true` is present inside `[features]`
- preserves unrelated content and comments as much as possible

Important constraint:

- **Install should enable the feature**
- **Uninstall should not try to disable it**

That avoids clobbering user intent or other tools that may also depend on hooks.

### Step 3: Add OM-managed `hooks.json` install/uninstall helpers

**File:** `src/observational_memory/cli.py`

Add helpers to:

- load existing `~/.codex/hooks.json` if present
- merge OM-owned hook groups into the existing JSON
- preserve unrelated hook groups from the user or other tools
- remove only OM-owned hook groups on uninstall

The safest ownership model is:

- identify OM groups by exact command + status message + matcher combination
- never replace unrelated groups in the same event array

Recommended initial OM-managed events:

- `SessionStart`
- `Stop`

### Step 4: Add transcript-specific Codex observation

**Files:**

- `src/observational_memory/observe.py`
- `src/observational_memory/cli.py`

Add `observe_codex_transcript(transcript_path)` parallel to `observe_claude_transcript()`.

Then fix the CLI contract so `om observe --transcript ...` is not Claude-only anymore. Options:

1. honor `--source codex` when `--transcript` is present
2. auto-detect transcript format from the file contents/path

I recommend:

- **explicit source first**
- auto-detect only as a convenience fallback

This is the key enabler for efficient Codex hooks. Without it, the stop hook would need to rescan all recent sessions.

### Step 5: Add a Codex checkpoint runner

**Preferred file:** `src/observational_memory/cli.py`

Add a hidden command, for example:

```bash
om codex-checkpoint
```

Behavior:

- read hook payload from `stdin`
- extract `transcript_path`, `hook_event_name`, `cwd`, `session_id`
- if there is no usable transcript, exit `0`
- maintain lock/state under `~/.local/share/observational-memory/`
- spawn `om observe --transcript "$TRANSCRIPT" --source codex` in the background
- record success/failure and latest observed message count

Why a hidden CLI command instead of a shell script:

- easier to test
- no `jq` dependency
- stable command target for hooks (`om ...`) even across package upgrades

Using a shell script like the Claude hooks is still viable, but a hidden CLI command is the better fit here because Codex already hands us structured JSON on `stdin` and the logic is more test-heavy than shell-heavy.

### Step 6: Rewrite Codex install mode around hooks-first defaults

**File:** `src/observational_memory/cli.py`

Change `om install --codex` semantics from:

- "write AGENTS.md additions"

to:

- enable hooks feature in `config.toml`
- install OM hook groups in `hooks.json`
- install a conditional AGENTS fallback block
- keep cron backstop unless `--no-cron`

Recommended CLI shape:

```bash
om install --codex
om install --codex --codex-mode hooks
om install --codex --codex-mode agents
om install --codex --codex-mode auto
```

Suggested meanings:

- `auto` (default): hooks-first, plus AGENTS fallback, plus cron backstop
- `hooks`: install hooks and fail loudly if hook setup cannot be completed
- `agents`: legacy behavior, skip hooks entirely

This keeps the current path available while making the new path the default.

### Step 7: Update uninstall semantics

**File:** `src/observational_memory/cli.py`

`om uninstall --codex` should:

- remove OM-managed entries from `~/.codex/hooks.json`
- remove the OM AGENTS fallback block
- remove OM cron jobs as it does today
- **not** remove `[features].codex_hooks = true`

If `hooks.json` becomes empty after removing OM-managed hooks, we can either:

- delete the file, or
- leave `{ "hooks": {} }`

Deleting it is cleaner.

### Step 8: Expand status and doctor for Codex hooks

**Files:**

- `src/observational_memory/cli.py`
- `tests/test_cli_doctor.py`

Add explicit Codex hook checks:

- `config.toml` exists / missing
- `codex_hooks` feature enabled / disabled
- `hooks.json` exists / missing
- `SessionStart` hook installed / not installed
- `Stop` hook installed / not installed
- AGENTS fallback present / missing
- hook command target resolves to a real `om` binary

Suggested reporting model:

- `PASS`: hooks installed and AGENTS fallback present
- `WARN`: hooks missing but AGENTS fallback present
- `FAIL`: neither hooks nor AGENTS fallback present

### Step 9: Update README and maintainer docs

**Files:**

- `README.md`
- `docs/MAINTAINERS.md`

Document the new reality clearly:

- Codex now uses hooks-first startup/checkpointing
- AGENTS is conditional fallback, not primary startup mechanism
- cron remains a backstop
- hooks are experimental in Codex and require `codex_hooks = true`

Also update wording that currently says:

- "Codex CLI integration = AGENTS.md + cron"

That should become:

- "Codex CLI integration = hooks-first + AGENTS fallback + cron backstop"

## Recommended Landing Order

### PR 1: Startup path only

Land the lowest-risk, highest-value slice first:

- config path additions
- `config.toml` feature enablement
- `hooks.json` `SessionStart` installation
- conditional AGENTS fallback rewrite
- status/doctor updates for startup integration

Why first:

- biggest UX improvement immediately
- lowest implementation risk
- uses the already-existing `om context`
- proves hook installation/merge behavior before adding checkpoint logic

### PR 2: Transcript-specific Codex checkpointing

Then land:

- `observe_codex_transcript()`
- hidden Codex checkpoint runner
- `Stop` hook installation
- tests for transcript-specific observe and background hook behavior

Why second:

- this is the new behavioral surface with the most moving parts
- it benefits from having hook install machinery already stabilized

### PR 3: Docs and polish

Finally:

- README cleanup
- maintainer docs
- any final naming or CLI polish after the runtime settles

## Critical Design Decisions

### Use global hooks, not repo-local hooks

OM is explicitly user-level shared memory, so the install target should be:

- `~/.codex/config.toml`
- `~/.codex/hooks.json`
- `~/.codex/AGENTS.md`

Repo-local `.codex/hooks.json` is the wrong default because it would fragment memory behavior across repos.

### Keep AGENTS, but make it conditional

Removing AGENTS entirely would leave older Codex builds and hook-disabled setups cold.

Keeping the current unconditional AGENTS text would likely duplicate startup context.

Conditional AGENTS is the best compromise.

### Keep cron, even after hooks land

Because hooks are experimental, the polling path should remain installed unless the user opts out.

This is the operational safety net, not legacy debt.

### Do not use `PreToolUse` / `PostToolUse` in v1

Those events are interesting, but they do not directly help observational memory.

Using only `SessionStart` + `Stop` keeps the first implementation easy to reason about.

## Test Plan

### Unit tests

**Files:**

- `tests/test_cli_install.py`
- `tests/test_cli_context.py`
- `tests/test_transcripts.py`
- `tests/test_cli_doctor.py`

Add coverage for:

- installing hooks when `config.toml` does not exist
- upserting `codex_hooks = true` into an existing `[features]` table
- appending a new `[features]` table when it is missing
- merging OM hooks into an existing `hooks.json` without dropping third-party hooks
- uninstall removing only OM-managed hooks
- conditional AGENTS fallback block generation
- `observe --transcript ... --source codex`
- codex checkpoint runner ignoring missing transcript paths cleanly

### Behavioral smoke tests

1. `om install --codex --no-cron --provider openai --llm-model gpt-4o-mini --non-interactive`
2. verify `~/.codex/config.toml` now has `[features].codex_hooks = true`
3. verify `~/.codex/hooks.json` contains `SessionStart` and `Stop`
4. verify `~/.codex/AGENTS.md` contains fallback wording, not unconditional startup reads
5. run `om status`
6. run `om doctor`
7. simulate `SessionStart` by piping a small hook payload into the installed command and confirm it emits `additionalContext`
8. simulate `Stop` with a Codex fixture transcript and confirm the cursor advances only for that transcript

### Full validation

```bash
uv run pytest
uv run ruff check .
```

## Sources

- OpenAI Codex hooks docs: <https://developers.openai.com/codex/hooks>
- OpenAI Codex AGENTS docs: <https://developers.openai.com/codex/guides/agents-md>
