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

Hermes has two supported paths:

- direct `om` transcript ingestion for existing Hermes session logs;
- the standalone [hermes-observational-memory](https://github.com/intertwine/hermes-observational-memory) memory-provider plugin for live Hermes context, search, recall, and explicit writes.

The plugin is installed through Hermes, not through `om install`:

```bash
hermes plugins install intertwine/hermes-observational-memory --no-enable
hermes memory setup
```

Choose `observational_memory` in the memory setup flow. Hermes memory providers are exclusive plugins, so activation happens through `memory.provider` instead of `plugins.enabled`.

The plugin pins its own supported `observational-memory` version range — check [the plugin repo](https://github.com/intertwine/hermes-observational-memory) for the current line before upgrading `om` on a Hermes host. Supported recent Hermes builds discover the plugin from `$HERMES_HOME/plugins/observational_memory`; no source-tree symlink is needed.

The plugin adds:

- `om_context` for compact startup context and optional task-specific recall;
- `om_search` for search over OM observations and reflections;
- `om_remember` for explicit durable observations;
- optional Hermes session writeback into OM observations;
- optional OM Cluster pull-before-context when cluster mode is enabled and `sync_before_context` is true.

For more setup and validation detail, see [Hermes plugin](hermes-plugin.md).

Manual observe path:

```bash
om observe --source hermes
om observe --transcript ~/.hermes/sessions/session-123.jsonl --source hermes
```

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

## OpenCode

OpenCode supports local plugins from `~/.config/opencode/plugins/` and global rules from `~/.config/opencode/AGENTS.md`. OM uses both. The plugin records message events into OM-owned JSONL files under the memory directory. The AGENTS fallback tells OpenCode how to load bounded startup context without bulk-reading generated memory files.

Install it:

```bash
om install --opencode
```

Use it:

```bash
om context --for opencode --cwd "$PWD"
om observe --source opencode
om recall --query "what did we decide about the OpenCode harness?"
```

Notes:

- The plugin is global, not project-local, so memory follows the user across repos.
- OpenCode event shapes can change. The parser is defensive and ignores unknown events.
- The fallback block lives in `~/.config/opencode/AGENTS.md` and can be removed with `om uninstall --opencode`.

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
