# Native Claude And Codex Memory

The native-memory bridge gives Claude Code and Codex one local place to search the memory summaries they already maintain. It is the shortest OM setup when you want useful cross-agent recall without a transcript pipeline.

The bridge is available on macOS in v0.10.0. It reads approved native summaries, builds a private BM25 index, and refreshes it every 15 minutes. It does not read raw transcripts, call an LLM, run reflection, change native memory, or upload it.

## Quick Start

Install or upgrade OM, then enable the bridge:

```bash
brew install intertwine/tap/observational-memory   # use `brew upgrade observational-memory` if already installed
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
om bridge-native-memory
om search --native-bridge "what were we doing in this project?"
om status
om doctor
```

`om native-bridge sources` lists eligible Claude project directory names with Markdown-file counts. It also reports whether the fixed Codex allowlist files are present. It never prints memory text or enrolls a source. The first install needs macOS and at least one exact Claude project name from that list. Repeat `--claude-project` to include more than one. Codex memory is added whenever either fixed Codex file is present.

Bridge retrieval is explicit. Ordinary `om search` and `om recall` continue to use the full OM memory store and do not merge bridge results.

## Choose The Right OM Path

| | Native-memory bridge | Full OM workflow |
| --- | --- | --- |
| Best for | Searching summaries Claude Code and Codex already maintain | Building OM's own memory from supported agent sessions |
| Input | Fixed native-memory allowlist | Supported agent transcripts and checkpoints |
| LLM needed | No | Yes, for observation and reflection |
| Retrieval | `om search --native-bridge` | `om recall`, `om search`, `om context`, and `om talk` |
| Background work | One bounded bridge check every 15 minutes | Observer, auto-memory, and reflector schedules |
| v0.10.0 platforms | macOS | macOS, Linux, and Windows, depending on integration |

You can move between these paths. The switch is explicit so an upgrade cannot silently restart transcript writers.

## Install Or Upgrade

Homebrew:

```bash
brew install intertwine/tap/observational-memory
# Existing install: brew upgrade observational-memory
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

`uv`:

```bash
uv tool install observational-memory
# Existing install: uv tool upgrade observational-memory
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

No provider login or API key is needed.

`--claude-project` on `om install --native-bridge` replaces the complete saved selection. To change it, repeat every project you want scheduled runs to keep:

```bash
om install --native-bridge \
  --claude-project "<first-project>" \
  --claude-project "<second-project>"
```

The same flag on `om bridge-native-memory` changes only that one refresh. The next scheduled run uses the selection saved by the installer.

On a v0.9.1 full install, bridge activation:

- boots out the OM Claude observer, Codex observer, Claude auto-memory, and reflector services;
- removes OM-managed Codex Stop, Claude checkpoint, and Cowork writer hooks;
- keeps read-only SessionStart context and fallback behavior;
- preserves unrelated hook groups and existing memory files;
- leaves the older service plist files disabled for an explicit return later.

Grok, Kimi, and OpenCode integrations are outside this Claude↔Codex migration. If you installed their writer integrations, they are unchanged.

If setup fails, OM attempts to restore prior service and managed-file state. It reports `rollback incomplete` if any restoration step fails so you know the install needs manual inspection.

## Run A Refresh Now

The service normally checks every 15 minutes. Run the same bounded bridge path immediately when you do not want to wait:

```bash
om bridge-native-memory
```

Then search it:

```bash
om search --native-bridge "decision about the release"
```

If no verified index exists yet, bridge search exits with guidance to install the bridge or run a refresh. It does not fall back to the full OM index.

## Source And Privacy Boundary

The bridge uses a fixed source allowlist:

- Codex: exactly `~/.codex/memories/MEMORY.md` and `~/.codex/memories/memory_summary.md`;
- Claude Code: `.md` files under `~/.claude/projects/<exact-project-name>/memory/` for the project directory names you selected.

There is no public Codex filename override. Claude selection accepts exact project directory names, not arbitrary paths, recursive user globs, or remote sources.

The bridge excludes:

- `raw_memories.md`;
- transcript and session directories;
- OM `observations.md` and `reflections.md`;
- non-Markdown files;
- files outside the approved native-memory roots.

Source roots are read-only. Inputs must be regular, user-owned files that stay unchanged while OM reads them. A symlink, unsafe permission or ownership state, file replacement, or unstable read causes that refresh to fail closed instead of widening access.

Output goes to a separate private local BM25 store. It is derived search data, not a new source of truth, and it is not sent through OM Cluster or OM Mail. The bridge does not use the configured QMD, QMD hybrid, Moss, or remote search backend. It does not load provider credentials or the OM provider environment for a bridge run.

## How A Scheduled Run Stays Bounded

The OM-owned macOS service is `com.intertwine.observational-memory.native-bridge`. It wakes every 15 minutes and runs only when the current machine state passes the bridge's admission checks.

Every attempt has fixed limits for:

| Check | v0.10.0 limit |
| --- | --- |
| macOS memory pressure | `normal` only |
| Swap use | At most 80% |
| Total runtime | 15 seconds |
| Process-tree memory | 128 MiB RSS |
| One input file | 2 MiB |
| All selected input | 16 MiB |

These ceilings cannot be raised through normal bridge options or OM provider settings. If admission fails or a limit is reached, the bridge keeps the last verified index. A busy index also fails safely instead of starting a competing writer.

## Check Health

Use these commands after install, upgrade, or troubleshooting:

```bash
om native-bridge status
om status
om doctor
```

`om native-bridge status` and `om status` report bridge configuration, service state, and verified-index readiness. `om doctor` also verifies that the older Claude, Codex, auto-memory, and reflector services and OM-managed writer hooks remain off.

None of these commands prints indexed memory text.

## Disable, Uninstall, Or Roll Back

Pause scheduled refresh but keep the service definition:

```bash
om native-bridge disable
om status
```

Re-enable it with `om install --native-bridge`; the saved Claude project selection is reused.

Stop scheduled bridge work and remove only its service:

```bash
om uninstall --native-bridge
om status
om doctor
```

This is the preferred feature-level rollback. Disable and uninstall both leave Claude Code and Codex native memory untouched. They also preserve the private bridge config, derived index data, and receipts. Removing the bridge does not restart the older OM writer jobs.

Remove the service and all bridge-derived local state while keeping source memory untouched:

```bash
om uninstall --native-bridge --purge
```

OM first boots out the exact bridge service and verifies that it is absent. It then removes only the saved selection, derived generations, receipts, and bridge logs.

Re-enable the bridge later:

```bash
om install --native-bridge
om bridge-native-memory
```

Or return explicitly to the full Claude Code and Codex workflow:

```bash
om uninstall --native-bridge
om install --both
om install --cowork   # only if you want Cowork writers restored too
om doctor
```

`--both` restores Claude Code and Codex only. The full installer may ask for an LLM provider because observation and reflection use one.

If you installed OM with `uv` and need to roll back the package itself after disabling the bridge:

```bash
om uninstall --native-bridge
uv tool install --force "observational-memory==0.9.1"
om status
```

The older writer jobs remain disabled until you explicitly run an install target such as `om install --both`.

## Troubleshooting

### The installer says the platform is unsupported

The v0.10.0 bridge is macOS-only. Both its service and one-shot command depend on macOS resource admission. The rest of OM can still be installed on Linux or Windows.

### Search says no bridge index exists

Run:

```bash
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
om bridge-native-memory
om status
om doctor
```

The search command will not borrow results from the ordinary OM index while the bridge has no verified index.

### A refresh was skipped for resource pressure

The bridge keeps the previous verified index. Let other heavy work finish, then retry:

```bash
om bridge-native-memory
om status
```

Do not raise or bypass the safety ceilings.

### The service is installed but not loaded

Re-run the idempotent installer, then inspect health:

```bash
om install --native-bridge
om status
om doctor
```

If activation fails, OM attempts to restore the pre-attempt service and managed-file state. It reports `rollback incomplete` if any restoration step fails.

### Native files are rejected

Do not replace them with symlinks or broadly loosen permissions. Let Claude Code or Codex rewrite the native memory normally, then run the bridge again. If the error persists, use `om doctor` to capture the non-sensitive reason before reporting a bug.

### Old transcript writers appear active

Run `om install --native-bridge` again. A successful activation removes OM-managed Claude, Codex, and Cowork writer hooks and boots out the four older writer services. Grok, Kimi, and OpenCode remain outside this migration scope; disable those integrations separately if you do not want them writing to the full OM workflow.

### An external Hermes or Grok plugin blocks the upgrade

Use Hermes memory-provider plugin v1.5.1 or newer and Grok marketplace plugin v0.1.2 or newer with OM v0.10. Their compatibility releases are validated separately from the core package.
