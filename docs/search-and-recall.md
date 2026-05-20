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
om context --for claude --cwd "$PWD" --task "prepare v0.6.4 release notes"
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
| `bm25` | Yes | Built-in keyword search. No extra setup. |
| `qmd` | No | QMD keyword search. |
| `qmd-hybrid` | No | QMD keyword plus vector search. |
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
