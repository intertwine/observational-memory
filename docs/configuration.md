# Configuration

Most users only need `om install`. This page explains the knobs behind it.

## Env File

The main config file is:

```bash
~/.config/observational-memory/env
```

On Windows:

```text
%APPDATA%\observational-memory\env
```

`om install` creates this file with owner-only permissions. The CLI loads it at startup, including when hooks and scheduled jobs call `om`.

Environment variables already set in your shell win over values in the file.

## Provider Settings

Direct Anthropic:

```bash
OM_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
OM_LLM_MODEL=claude-sonnet-4-5-20250929
```

Direct OpenAI:

```bash
OM_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OM_LLM_MODEL=gpt-4o-mini
```

Anthropic on Vertex AI:

```bash
OM_LLM_PROVIDER=anthropic-vertex
OM_VERTEX_PROJECT_ID=my-gcp-project
OM_VERTEX_REGION=us-east5
OM_LLM_MODEL=claude-sonnet-4-5-20250929
```

Anthropic on Bedrock:

```bash
OM_LLM_PROVIDER=anthropic-bedrock
OM_BEDROCK_REGION=us-east-1
OM_LLM_MODEL=anthropic.claude-sonnet-4-5-20250929-v1:0
```

Model precedence:

1. `OM_LLM_OBSERVER_MODEL` or `OM_LLM_REFLECTOR_MODEL`
2. `OM_LLM_MODEL`
3. provider default

## Memory Paths

Default local memory:

```bash
~/.local/share/observational-memory/
```

Windows default:

```text
%LOCALAPPDATA%\observational-memory\
```

Override with XDG paths:

```bash
export XDG_DATA_HOME=~/my-data
export XDG_CONFIG_HOME=~/my-config
```

Important files:

- `observations.md`: recent notes
- `reflections.md`: long-term memory
- `profile.md`: stable startup context
- `active.md`: current startup context
- `.cursor.json`: transcript checkpoints
- `.search-index/`: local search index

## Startup Controls

Control generated profile sections:

```bash
OM_PROFILE_INCLUDE_IDENTITY=0
OM_PROFILE_SECTIONS=preferences,relationship,key-facts
```

These settings only narrow generated profile/startup output. They do not turn off observation, reflection, search, recall, or cluster sync.

## Reflection Metadata

Reflection entries use inline comments like:

```markdown
- Prefer short status updates <!--om: id=ome_abc kind=preference actionability=medium scope=cluster-->
```

Common fields:

- `kind`: `snapshot`, `evergreen`, `preference`, `policy`, `identity`, `task`, `decision`, or `mode`
- `actionability`: `low`, `medium`, or `high`
- `sensitivity`: `normal` or `personal`
- `confidence`: usually `medium`
- `scope`: `cluster` or `local`
- `last_seen`, `last_verified`, `expires`, and `seen_count`

Unknown fields are preserved.

## Schedules

Default schedules:

- Codex observer backstop: every 15 minutes
- Claude auto-memory scan: hourly
- reflector: daily at 04:00 local time

Tune Codex polling:

```bash
OM_CODEX_OBSERVER_INTERVAL_MINUTES=10
```

Tune in-session checkpoints:

```bash
OM_SESSION_OBSERVER_INTERVAL_SECONDS=900
OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS=0
```

## Search Backend

Default:

```bash
OM_SEARCH_BACKEND=bm25
```

Optional QMD:

```bash
OM_SEARCH_BACKEND=qmd-hybrid
OM_QMD_INDEX_NAME=observational-memory
OM_QMD_NO_RERANK=1
```

See [search-and-recall.md](search-and-recall.md) for QMD setup.

## Cluster Flags

Cluster mode is off until local cluster config and keys exist.

Force cluster off for one command:

```bash
OM_CLUSTER_ENABLED=0 om context
```

Useful cluster env overrides:

```bash
OM_CLUSTER_ENABLED=1
OM_CLUSTER_SYNC_BEFORE_CONTEXT=1
OM_CLUSTER_STARTUP_PULL_DEADLINE_MS=1500
```

Relay and filesystem transports remain untrusted. Cluster trust comes from local keys, signatures, membership records, and approval state.
