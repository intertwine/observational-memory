# Release Notes - v0.9.1

## Theme

v0.9.0 put background observation in a bounded worker lane. v0.9.1 closes the paths that could still slip past it.

This patch release follows real trouble on a live machine. A profiling pass found two ways work could slip past the bounded lane: installed Claude hooks could still spawn unbounded direct observe processes through a shell script, and transcript scanning loaded whole files into memory. A separate incident then drove the third change: a background `om observe-worker` process grew to nearly 5 GB of memory under swap pressure, showing that a time limit alone is not enough. v0.9.1 fixes all three — bounded Claude checkpoints, streaming scans, and a memory ceiling on the worker lane. Upgrading is the only action needed.

Everything remains local-first. No new integrations, no config migrations, and no file-format changes.

## Claude checkpoints use the bounded lane everywhere

`om install --claude` now wires the `SessionEnd`, `UserPromptSubmit`, and `PreCompact` hooks to `om claude-checkpoint` on every platform. Windows already worked this way; on macOS and Linux these events previously ran a shell script that spawned direct `om observe` processes outside the bounded lane.

The bundled `session-end.sh` script remains as a fallback that hands the event to `om claude-checkpoint`, so settings that still point at the old script keep working after a package upgrade (see Compatibility below for one Homebrew caveat).

**Upgrade step that matters:** run `om install --claude` after upgrading so your installed hooks call `om claude-checkpoint` directly.

## Background workers have a memory ceiling

```bash
OM_OBSERVER_WORKER_MAX_RSS_MB=4096
```

Observer work now runs in a child process on every platform. The parent samples the child's memory about once per second — with `ps` on macOS and Linux, and with `tasklist` on Windows — and stops a worker once a sample shows it over the ceiling, in addition to the existing time limit. This is a sampled check, not a hard allocation limit: a very fast-growing worker can pass the ceiling between samples, and on some non-English Windows systems the memory reading cannot be parsed, so the ceiling is not enforced there.

A worker stopped for memory is recorded as `memory_exceeded`, distinct from `timeout`, so you can tell which limit fired. Checkpoint state lives in your memory directory: `.session-observer-state.json` for Claude and `.codex-checkpoint-state.json` for Codex. Set `OM_OBSERVER_WORKER_MAX_RSS_MB=0` to disable the ceiling.

## Transcript scanning streams instead of loading files

- Claude JSONL parsing, message counting, and last-UUID lookup now read line by line instead of loading the whole transcript.
- Codex JSONL parsing streams from the message cursor, and message counting no longer retains parsed message objects.
- Saved Codex reading positions ("cursors") from older releases now carry over correctly when they land exactly at the end of a transcript. Earlier versions could re-scan the whole file in that case. No action is needed.
- Cursor migration and the streaming parser now share one record-expansion helper, so they agree on how a transcript line splits into records.

Large active transcripts still get scanned for counts and cursors; this release bounds that work and removes full-file reads, it does not eliminate scanning.

## Upgrade

```bash
brew upgrade observational-memory   # or: uv tool upgrade observational-memory
om install --claude
om doctor
```

`om doctor` confirms the four Claude hooks exist, but it does not check which command they run. To confirm the switch, open `~/.claude/settings.json` and check that the checkpoint hooks call `om claude-checkpoint`.

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest    # 1110 passed
```

## Compatibility

Fully compatible with v0.9.0. If you skip `om install --claude`, hooks that point at the bundled script keep working through the updated fallback — as long as that script path still exists after the upgrade. Homebrew installs can lose old versioned paths during cleanup, and the hook then does nothing until you run `om install --claude` (or `--all`). Cowork's separate session-end hook is not yet in the bounded lane; it is unchanged in this release.
