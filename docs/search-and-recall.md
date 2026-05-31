# Search, Recall, And Startup Context

Startup context should be small. Recall can be larger. That split is the main idea behind `om context`, `om recall`, and `om search`.

## Startup Context

Agents call:

```bash
om context
```

The command returns JSON for a session-start hook. The JSON contains a bounded `additionalContext` string.

Route the startup pack when the host can provide more detail:

```bash
om context --for codex --cwd "$PWD" --task "fix cluster validation docs"
om context --for claude --cwd "$PWD" --task "prepare v0.6.6 release notes"
```

Control the budget:

```bash
om context --budget-chars 16000
```

If useful sections do not fit, the output lists expansion handles such as:

```text
startup:active:active-projects
startup:profile:preferences-opinions
```

For large memory corpora, startup context is a projection rather than a full dump.
`om context` can emit a compact `Working Profile`, strip inline OM provenance comments from startup output, and split active context by project-level subsections. The full generated Markdown remains available through recall handles.

### Quality: freshness, dedup, and scope

As your memory grows, startup context applies three quality passes so the budget is spent on signal, not noise:

- **Deduplication.** A bullet that appears in more than one section is shown once, in the highest-priority section. (Per-project Active Projects fields like `Status` or `Owner` are kept distinct — only repeated profile guidance is collapsed.)
- **Freshness.** Operational facts (tool versions, install status) older than `OM_STARTUP_FRESHNESS_DAYS` (default `14`) get an `(as of <date> — verify)` marker, so the agent knows whether to trust the value or check live. Durable preferences and identity facts are never marked.
- **Scope.** The `--cwd`/`--task` you pass (the SessionStart hook passes them) give the matching project first claim on the budget; unrelated active-project inventory overflows to recall handles instead of crowding out the current work.

Inspect all three with the diagnostic:

```bash
om context --quality-report          # human-readable
om context --quality-report --json   # machine-readable
```

It reports duplicate bullets dropped, operational facts that look stale, budget usage per included section, and what overflowed to recall. See [`configuration.md`](configuration.md#quality-freshness-dedup-and-scope) for details.

## Recall

Use a handle when startup context points to an omitted section:

```bash
om recall --handle startup:active:active-projects
om recall --handle startup:active:active-projects:observational-memory
om recall --handle startup:profile
```

Use a query when you need deeper memory:

```bash
om recall --query "what did we decide about relay trust?"
om recall --query "current release checklist" --limit 5 --json
```

`om recall` is meant for agents and plugins as well as humans. It accepts the same routing hints as `om context`:

```bash
om recall --query "current work" --for codex --cwd "$PWD" --task "release docs"
```

## Search

`om search` is the lower-level search command:

```bash
om search "PostgreSQL setup"
om search "preferences" --limit 5
om search "launchd" --json
om search "preferences" --reindex
```

Use `om recall` for agent-friendly retrieval. Use `om search` when you want direct search results and source metadata.

## Search Backends

| Backend | Default | What it does |
| --- | --- | --- |
| `bm25` | Yes | Built-in keyword search. No extra setup, fully local. |
| `qmd` | No | QMD keyword search. |
| `qmd-hybrid` | No | QMD keyword plus vector search. |
| `moss` | No | [Moss](https://www.moss.dev) cloud semantic search. Opt-in; uploads memory text. See [Talk to your memories](talk-to-memories.md). |
| `none` | No | Disables search. |

Set the backend in your env file:

```bash
OM_SEARCH_BACKEND=bm25
```

QMD example:

```bash
OM_SEARCH_BACKEND=qmd-hybrid
OM_QMD_INDEX_NAME=observational-memory
om search --reindex "test query"
qmd --index observational-memory embed
om search "current project status"
```

For faster QMD hybrid lookup on QMD versions that support it:

```bash
OM_QMD_NO_RERANK=1
```

## Metadata In JSON Output

`om search --json` can include:

- `source_path`
- `source_line`
- `qmd_file`
- `qmd_docid`
- `qmd_line`
- parsed metadata from the source document

That makes it useful for agents that need source references.
