---
name: om
description: >
  Cross-session observational memory for the Aside agentic browser. Use when the
  user asks to "recall something from a past session", "search memory", "what do
  you know about me", "warm start from om", or "write this session to om". Also
  triggers on "observational memory", "om", or "Aside<->om".
autoInject:
  keywords:
    - observational-memory
    - observational memory
    - om context
    - om observe
    - warm start
---

# Observational Memory (om) for Aside

[Observational Memory](https://github.com/intertwine/observational-memory) (`om`)
is a local, Markdown-based memory layer shared across coding agents. Aside is a
first-class **observe-first** peer: it reads warm context from om at session
start and writes its own session transcripts back into om, where the real `om`
process consolidates them.

Aside has no native hooks, so this skill is the integration. Run every `om`
command through the **Bash tool** (om needs filesystem access outside the agent
root, which the REPL `fs` cannot reach).

## Environment

- `om` is on `PATH` (installed via `uv tool install observational-memory` or
  Homebrew). If it is not, use the absolute binary path.
- Memory stores live in om's data dir (default
  `~/.local/share/observational-memory/`): `profile.md`, `active.md`,
  `observations.md`, `reflections.md`.
- Aside sessions are auto-discovered from
  `~/.aside/u/<idx>/agents/<agent>/sessions/<date>_<id>/messages.jsonl`.
- Aside's source label is **`aside`** (`--source aside`, `--for aside`).

## Warm start (read context)

```bash
om context --for aside --cwd "$PWD"          # budgeted startup pack (JSON)
```

For richer detail, read the raw stores directly:

```bash
D="$(om config get data_dir 2>/dev/null || echo "$HOME/.local/share/observational-memory")"
cat "$D/profile.md" "$D/active.md"
om recall --query "<topic>" --limit 10 --json   # deeper recall
```

If `om recall`/`om search` are unavailable in the environment (optional `qmd`
search backend not installed), fall back to `grep -i "<topic>" "$D/observations.md" "$D/reflections.md"`.

## Write back (save the session)

om now parses Aside's native `messages.jsonl` directly — no hand-built transcript
needed. At end of session, point om at the current session transcript, or let it
auto-discover recent ones:

```bash
om observe --source aside --dry-run                       # preview all recent Aside sessions
om observe --source aside                                 # ingest; auto-runs the reflector

# or target one session explicitly:
SESSION=~/.aside/u/0/agents/main/sessions/<date>_<id>/messages.jsonl
om observe --transcript "$SESSION" --source aside --dry-run
om observe --transcript "$SESSION" --source aside
```

Resumption is cursor-based per transcript path, so re-running is idempotent: a
second `--dry-run` should report "No new messages to process." Take a safety
snapshot before bulk writes with `om backup`.

## Verify

- `om status` shows an **Aside** section with the sessions dir and discovered count.
- A new dated block tagged `aside` appears in `observations.md` after `om observe`.

## Gotchas

- A session needs at least 5 user+assistant messages or the observer is skipped.
- `thinking` blocks and `toolResult`/`system-message` records are intentionally
  dropped; tool calls survive as one-line summaries.
- Don't change the user's om provider/search-backend env without asking.
