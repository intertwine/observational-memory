# Platform Integrations

Observational Memory is user-level memory. It is shared across the agents you install it for, instead of being tied to one project checkout.

## Claude Code

Install:

```bash
om install --claude
```

What gets installed:

- `SessionStart` hook: calls `om context`
- `SessionEnd` hook: observes the transcript
- `UserPromptSubmit` and `PreCompact` hooks: run throttled checkpoints

Useful settings:

```bash
OM_SESSION_OBSERVER_INTERVAL_SECONDS=900
OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=0
```

Set `OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=1` to stop in-session checkpoints while keeping normal end-of-session capture.

## Codex

Install:

```bash
om install --codex
```

What gets installed:

- `~/.codex/config.toml`: enables hooks
- `~/.codex/hooks.json`: adds OM `SessionStart` and `Stop` hooks
- `~/.codex/AGENTS.md`: keeps a conditional fallback for older or hook-disabled sessions

Runtime behavior:

- `SessionStart` calls `om context`.
- `Stop` queues a transcript-specific checkpoint.
- A background observer remains as a backup.
- Startup context is budgeted, so large memory does not flood the session.
- Agents can expand more with `om recall`.

Check it:

```bash
om context --for codex --cwd "$PWD" --task "review current work"
om recall --query "current work" --for codex --cwd "$PWD"
```

## Claude Cowork

Install on macOS:

```bash
om install --cowork
```

The plugin is copied to:

```text
~/Library/Application Support/Claude/local-agent-mode-plugins/observational-memory/
```

It includes:

- startup context through `om context`
- checkpoint hooks
- an `observational-memory` skill
- a `/recall` command

Manual observe path:

```bash
om observe --source cowork
om observe --transcript "$HOME/Library/Application Support/Claude/local-agent-mode-sessions/.../audit.jsonl" --source cowork
```

## Hermes

Hermes support currently means transcript ingestion. `om install` does not install Hermes hooks yet.

Manual observe path:

```bash
om observe --source hermes
om observe --transcript ~/.hermes/sessions/session-123.jsonl --source hermes
```

The next Hermes work is a first-class external memory-provider plugin. The active handoff is [plans/hermes-first-class-plugin.md](../plans/hermes-first-class-plugin.md).

## Claude Code Auto-Memory

Claude Code can store per-project facts under:

```text
~/.claude/projects/*/memory/*.md
```

`om observe --source claude-memory` scans those files read-only. It does not call an LLM. Changed files go into the search index and can help the reflector build cross-project memory.

## ChatGPT And Claude Managed Agents

`om` does not silently write hosted product memory.

Use reviewed exports:

```bash
om export --target chatgpt
om export --target claude-managed-agents --output ./om-claude-memory
om export --target generic --include-observations
```

ChatGPT exports are concise seeds that you review before adding to ChatGPT or a project.

Claude Managed Agents exports are small Markdown files plus a manifest. They are designed for memory-store import or review.

## Grok Build TUI (xAI)

Grok has excellent native hook support and also reads `~/.claude/settings.json` for compatibility.

Install:

```bash
om install --grok
# or
om install --all
```

What gets installed:

- `~/.grok/hooks/observational-memory.json` — OM `SessionStart` (context) + checkpoint hooks
- On Windows: direct `om` executable invocations (no shell script dependency)
- If OM Claude hooks already exist in `~/.claude/settings.json`, the installer intelligently omits a duplicate `SessionStart` to avoid double-injecting context (Grok inherits it via the compatibility layer)

Runtime behavior:

- `SessionStart` → `om context` (search-backed, Cluster-aware, budgeted)
- `SessionEnd` / `UserPromptSubmit` / `PreCompact` → `om grok-checkpoint` (queues observation of the `updates.jsonl`)
- `om observe --source grok` ingests Grok sessions (parses the rich `session/update` JSONL format with proper chunk and tool handling)
- Large Grok sessions are automatically processed in safe batches

Check it:

```bash
om context --for grok --cwd "$PWD" --task "continue the current feature"
om recall --query "the feature we were implementing" --for grok
om doctor   # Look for the Grok Build TUI section
```

Useful commands:

```bash
om grok-checkpoint --transcript ~/.grok/sessions/.../updates.jsonl
om observe --source grok --dry-run
```

Grok also has its own experimental native memory (`~/.grok/memory/`). OM and Grok memory are independent peers — use OM when you want cross-agent (Claude + Codex + Grok + Hermes) shared memory.
