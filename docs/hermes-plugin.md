# Hermes Plugin

Observational Memory has a standalone Hermes memory-provider plugin:

```text
https://github.com/intertwine/hermes-observational-memory
```

Use it when you want Hermes to read the same local-first memory as Claude Code, Codex, Grok Build TUI, and Cowork during live sessions. Core `om` still supports Hermes transcript ingestion directly; the plugin adds live context and tools inside Hermes.

## Requirements

- Hermes with user-installed memory provider discovery. Recent Hermes releases discover providers from `$HERMES_HOME/plugins/<name>`.
- `observational-memory>=0.6.5,<0.7`.
- Optional: an initialized OM Cluster when you want shared memory across machines.

## Install

Install the plugin through Hermes:

```bash
hermes plugins install intertwine/hermes-observational-memory --no-enable
```

Then select it as the memory provider:

```bash
hermes memory setup
```

Choose `observational_memory`.

The `--no-enable` flag is intentional. Hermes memory providers are exclusive plugins, so Hermes activates them through `memory.provider`, not through `plugins.enabled`.

If Hermes does not install the Python dependency automatically, install it in the Hermes runtime:

```bash
uv pip install "observational-memory>=0.6.5,<0.7"
```

## What Hermes Gets

The provider adds three tools:

- `om_context`: load compact startup context, with optional task-specific recall.
- `om_search`: search OM observations and reflections.
- `om_remember`: store an explicit durable observation.

It also supports optional Hermes session writeback into OM observations.

## OM Cluster

OM Cluster is opt-in. The plugin only syncs when the local OM install is already initialized, enabled, and configured for startup pull.

Check cluster state:

```bash
om cluster status
om cluster sync
```

To let Hermes pull shared records before reading startup memory, enable `sync_before_context` in the OM cluster config or set:

```bash
OM_CLUSTER_SYNC_BEFORE_CONTEXT=1 hermes
```

When cluster mode is active, `om_remember` writes a signed OM Cluster observation record and materializes the local Markdown view. If `sync_on_observe` is enabled, it also performs a short post-write sync.

## Validate

Basic checks:

```bash
hermes memory status
om doctor --validate-key
om cluster status
```

Inside Hermes, ask it to:

- call `om_context` for the current task;
- call `om_search` for a known memory;
- call `om_remember` with a short test note;
- confirm the note appears through `om search`.

For cross-machine validation, make a unique test note on one Hermes instance, sync the OM Cluster, and confirm the other Hermes instance can recall it through `om_context` or `om_search`.

## Transcript Ingestion Still Works

The plugin is not required for log ingestion. You can still observe Hermes sessions directly:

```bash
om observe --source hermes
om observe --transcript ~/.hermes/sessions/session-123.jsonl --source hermes
```

Do not sync the whole OM memory directory between machines. Use OM Cluster transports instead.
