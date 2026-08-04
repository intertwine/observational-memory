# Release Notes - v0.10.0

## Theme

v0.10.0 adds a native-first way for Claude Code and Codex to share useful memory on a Mac.

The native-memory bridge reads the small summaries that those agents already maintain, selects only its approved sources, and builds a private local BM25 index. This gives you a shared retrieval layer without turning on OM's transcript observers or reflection workflow.

The boundary is deliberate: the bridge does not ingest raw transcripts, call an LLM, run reflection, change the source memories, or silently upload memory.

## Get Started

For a new Homebrew install, run `brew install intertwine/tap/observational-memory`. If OM is already installed, run `brew upgrade observational-memory`.

Choose the native sources and enable the bridge:

```bash
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

If Claude Code or Codex was running during installation, save the current work, exit the affected app or CLI session, and start a new session. A running process can retain its previous OM write hooks until it restarts.

Build the first index and check the result:

```bash
om bridge-native-memory
om search --native-bridge "what were we doing in this project?"
om native-bridge status
om doctor
```

`om native-bridge sources` lists eligible Claude project names and counts without showing memory text or enrolling them. Repeat `--claude-project` to include more than one. Later installs and one-shot runs reuse the saved private selection when you omit the flag.

Ordinary `om search` and `om recall` keep using the full OM memory store. They do not merge bridge results. Before the first verified bridge index exists, `om search --native-bridge` exits with guidance to install or run the bridge.

The bridge is macOS-only in v0.10.0. Both its one-shot command and scheduled service use built-in macOS memory and swap checks.

## What The Bridge Reads

The fixed allowlist is Codex `MEMORY.md` and `memory_summary.md`, plus top-level `.md` files directly in the exact Claude project memory directories selected at install. It does not scan nested directories or arbitrary files elsewhere in Claude Code or Codex data directories.

Its source roots stay read-only. Raw-memory files, transcripts, session logs, and OM's own observation and reflection inputs are outside this path. The derived bridge index is not sent through OM Cluster or OM Mail.

See [Native Claude and Codex memory](native-memory-bridge.md#source-and-privacy-boundary) for the reader-facing source rules.

## Bounded Background Refresh

`om install --native-bridge` installs one OM-owned macOS service. It checks for changed source summaries every 15 minutes and publishes a new verified local index only after the run passes its safety checks.

Each run requires normal macOS memory pressure and at most 80% swap use. Admission, index building, publication, and in-deadline supervisor telemetry stop at 15 seconds, and the spawned bridge worker process tree is limited to 128 MiB RSS. After a timeout, OM can finish writing a local failure receipt, but that receipt cannot publish a generation. Input is limited to 2 MiB per file and 16 MiB total. If admission fails or a limit is reached, the bridge keeps the last verified index. `om native-bridge status`, `om status`, and `om doctor` report configuration, service, and verified-index state.

Enabling the bridge stops OM's older Claude and Codex background writers and removes OM-managed write hooks for Claude Code, Codex, and Cowork. Read-only startup context, unrelated hooks, and existing memory remain in place. If installation fails, OM restores the managed files and each legacy service that was explicitly enabled before the attempt. A service whose prior launchd state was `default` or could not be determined stays disabled for safety and appears in `om doctor`; OM asks for manual recovery only if a restoration step itself fails.

Grok, Kimi, and OpenCode integrations are outside this Claude↔Codex migration scope. Existing writer integrations for those hosts are unchanged.

After installation, start a new session for any Claude Code or Codex app or CLI session that was previously running, then run `om doctor` to confirm that the older OM writers remain off.

## Upgrade From v0.9.1

After upgrading the package, install the bridge and check the result:

```bash
brew upgrade observational-memory   # or: uv tool upgrade observational-memory
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

If Claude Code or Codex was running during installation, save the current work, exit the affected app or CLI session, and start a new session. Then build and check the index:

```bash
om bridge-native-memory
om native-bridge status
om doctor
```

No source-memory migration is required. Claude Code and Codex continue to own their native memory files; the bridge builds a separate index.

OM v0.10 is compatible with Hermes memory-provider plugin v1.5.1 or newer and Grok marketplace plugin v0.1.2 or newer. Those plugin releases are validated and published separately from the core package.

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
om install --cowork   # only if you want Cowork writers restored too
```

`--both` restores Claude Code and Codex only. Save current work, exit the affected Claude Code or Codex app or CLI session, and start a new session so it loads the restored hooks. Then run `om doctor`. To remove the bridge service plus its private config, derived index, receipts, and logs, run `om uninstall --native-bridge --purge`; source memory is untouched.

After purge, run `om native-bridge sources` and pass at least one exact project name with `--claude-project` when you enable the bridge again. Purge intentionally removes the saved selection.

## Other Fixes

- Codex legacy cursor migration now counts only the same non-empty messages that the transcript parser counts. Empty-content records can no longer advance a migrated cursor and cause later messages to be skipped.

## Current Limits

- macOS only in v0.10.0.
- Claude Code and Codex native memory summaries only.
- Local BM25 keyword retrieval only; this bridge does not use QMD, Moss, a remote backend, or an LLM.
- The bridge can index only summaries that the host agents have already written.
- Background freshness is normally within one 15-minute service interval. A scheduled failure uses a bounded retry delay; after the Mac passes admission again, use `om bridge-native-memory` when you need an immediate refresh.

For the complete operating guide, see [Native Claude and Codex memory](native-memory-bridge.md).
