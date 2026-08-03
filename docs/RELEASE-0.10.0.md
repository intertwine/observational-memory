# Release Notes - v0.10.0

## Theme

v0.10.0 adds a native-first way for Claude Code and Codex to share useful memory on a Mac.

The native-memory bridge reads the small summaries that those agents already maintain, selects only its approved sources, and builds a private local BM25 index. This gives you a shared retrieval layer without turning on OM's transcript observers or reflection workflow.

The boundary is deliberate: the bridge does not ingest raw transcripts, call an LLM, run reflection, change the source memories, or silently upload memory.

## Get Started

Install or upgrade OM on macOS, then enable the bridge:

```bash
brew install intertwine/tap/observational-memory   # use `brew upgrade observational-memory` if already installed
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
om bridge-native-memory
om status
om doctor
```

`om native-bridge sources` lists eligible Claude project names and counts without showing memory text or enrolling them. Repeat `--claude-project` to include more than one. Later installs and one-shot runs reuse the saved private selection when you omit the flag.

Retrieve bridge memory explicitly:

```bash
om search --native-bridge "what were we doing in this project?"
```

Ordinary `om search` and `om recall` keep using the full OM memory store. They do not merge bridge results. Before the first verified bridge index exists, `om search --native-bridge` exits with guidance to install or run the bridge.

The bridge is macOS-only in v0.10.0. Both its one-shot command and scheduled service use macOS resource admission.

## What The Bridge Reads

The fixed allowlist is Codex `MEMORY.md` and `memory_summary.md`, plus `.md` files inside the exact Claude project memory directories selected at install. It does not scan arbitrary files below Claude Code or Codex data directories.

Its source roots stay read-only. Raw-memory files, transcripts, session logs, and OM's own observation and reflection inputs are outside this path. The derived bridge index is not sent through OM Cluster or OM Mail.

See [Native Claude and Codex memory](native-memory-bridge.md#source-and-privacy-boundary) for the reader-facing source rules.

## Bounded Background Refresh

`om install --native-bridge` installs one OM-owned macOS service. It checks for changed source summaries every 15 minutes and publishes a new verified local index only after the run passes its safety checks.

Each run requires normal macOS memory pressure and at most 80% swap use. It is limited to 15 seconds, 128 MiB process-tree RSS, 2 MiB per file, and 16 MiB total input. If admission fails or a limit is reached, the bridge keeps the last verified index. `om native-bridge status`, `om status`, and `om doctor` report configuration, service, and verified-index state.

The explicit bridge install boots out the older Claude observer, Codex observer, Claude auto-memory, and reflector services, and never enables them. It also removes OM-managed Codex Stop, Claude checkpoint, and Cowork writer hooks while preserving read-only SessionStart context and unrelated hook groups. It keeps the older service plist files so you can make a deliberate return to the full workflow later. If bridge setup fails, the installer restores the exact service and file state it found before it started.

Grok, Kimi, and OpenCode integrations are outside this Claude↔Codex migration scope. Existing writer integrations for those hosts are unchanged.

## Upgrade From v0.9.1

After upgrading the package, install the bridge and check the result:

```bash
brew upgrade observational-memory   # or: uv tool upgrade observational-memory
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
om bridge-native-memory
om status
om doctor
```

No source-memory migration is required. Claude Code and Codex continue to own their native memory files; the bridge builds a separate index.

The standalone Hermes memory-provider and Grok marketplace plugins are released separately and their existing releases do not yet declare OM v0.10 compatibility. Check those plugin releases before upgrading OM on a host that depends on them.

## Disable Or Roll Back The Bridge

Pause scheduled refresh while keeping the service definition:

```bash
om native-bridge disable
```

Remove the bridge service while keeping v0.10.0 and all source memory:

```bash
om uninstall --native-bridge
om status
om doctor
```

Uninstall is the preferred feature-level rollback. Neither disable nor uninstall deletes Claude Code or Codex memory. Both preserve the private bridge config, derived index, and receipts. Re-enable the bridge later with `om install --native-bridge`; it reuses the saved Claude project selection.

Removing the bridge does not silently restart the older writer jobs. To return to the full Claude Code and Codex workflow, opt in explicitly:

```bash
om uninstall --native-bridge
om install --both
om doctor
```

## Current Limits

- macOS only in v0.10.0.
- Claude Code and Codex native memory summaries only.
- Local BM25 keyword retrieval only; this bridge does not use QMD, Moss, a remote backend, or an LLM.
- The bridge can index only summaries that the host agents have already written.
- Background freshness is normally within one 15-minute service interval; use `om bridge-native-memory` when you need an immediate refresh.

For the complete operating guide, see [Native Claude and Codex memory](native-memory-bridge.md).
