# Legacy status and migration

September 5, 2026. Observational Memory and its Intertwine-owned plugins are
**unmaintained legacy software**. The final core release is **0.10.1**.
No feature updates, compatibility updates, or security fixes are promised.
Existing source, package names, releases, and licenses remain available.
The repositories are retained for reference and forks, not ongoing support.

## What to use instead

Prefer the native memory features of your agent. Keep required project rules in
reviewed repository documentation or the host's instruction files. Keep historical
OM information as a private, readable archive that you consult when needed.

- [Codex](https://learn.chatgpt.com/docs/customization/memories) maintains local
  memory separately from ChatGPT memory. Use its supported memory controls; do not
  overwrite its generated memory store with an OM export.
- [Claude Code](https://code.claude.com/docs/en/memory) provides auto memory and
  authored `CLAUDE.md` instructions. Native memory files are machine-local.
- [Claude Chat and Cowork](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)
  have their own memory controls. Local Cowork and cloud Cowork are not identical.
  Cowork can access connected folders subject to permission; a cloud session can
  reach local files through an online desktop app. See the
  [architecture guide](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview).
- For Hermes and OpenClaw, retain or select the host's built-in memory facilities.
  Do not remove unrelated memory plugins or jobs just because they use words such
  as “memory,” “observer,” or “dreaming.”

Permission to read another agent's memory files is not automatic cross-vendor
recall, synchronization, or conflict resolution. Cloud agents need an explicit
access path to local information. OM Cluster was one such path, not a prerequisite
for exporting or sharing reviewed files. Retirement accepts losing seamless OM
cross-agent integration; it does not claim every native system has equivalent
recall quality or that all historical information was imported successfully.

## Preserve first

1. Inventory every OS user, virtual environment, profile, package manager, hook,
   plugin, cron entry, launchd/systemd job, relay and remote consumer. `which -a om`
   can reveal multiple PATH installations but does not find every environment.
2. Stop automatic OM writers and sync jobs using the owning host's service manager.
   Save current work before restarting an agent that has loaded an OM plugin.
3. Make a protected backup of the OM data directory, normally
   `~/.local/share/observational-memory`, including provenance and cluster records.
   Preserve configuration separately; it may contain credentials. Use owner-only
   permissions and verify backup contents and hashes. Do not publish backups.
4. Retain `observations.md`, `reflections.md`, `profile.md`, `active.md`, and useful
   provenance. A readable archive does not require an installed OM runtime.
5. If wanted, place a **copy** of the store at `<copy-root>/observational-memory`
   and run `XDG_DATA_HOME=<copy-root> om export --target generic --output <new-directory>`. Export can
   refresh derived startup files. It produces a review bundle, not a confirmed
   import into another provider. Curate and redact before sharing.

Never sync the live OM data directory wholesale to another machine. For a retained
legacy cluster, use its documented transport. Removing a node does not erase data
already received by another node or service.

## Remove active integrations

Use **core 0.10.1** for Claude hook removal: older uninstallers can remove
unrelated hooks from the same event. The final version removes only recognized
OM commands, keeps other entries, and creates a private settings backup before
changes. It refuses malformed or symlinked Claude settings for manual review.
Inspect unsupported custom wrapper commands yourself; do not delete an entire
hook group that also contains another tool's commands.

On macOS, remove an installed native bridge separately:

```sh
om uninstall --native-bridge
```

Then remove the selected core-managed integrations, without `--purge`:

```sh
om uninstall --all
```

`--all` covers core-managed Claude, Codex, Cowork, OpenCode, Kimi and Grok
integrations. It does **not** remove independently installed Hermes/Grok plugins,
custom systemd units, remote relays, or other users' installations. It does not
uninstall the Python/Homebrew package. Never use `--purge` to preserve the archive.

- **Hermes:** in every affected `HERMES_HOME`, select built-in memory using
  `hermes memory setup`, disable/remove `observational_memory`, then restart the
  affected gateway. Check `hermes memory status`. Older source-tree plugin links
  may need separate removal. Preserve `MEMORY.md`, `USER.md`, and other plugins.
- **Grok plugin:** run `/om-teardown` before uninstalling the plugin, or use the
  retained teardown script documented in the
  [plugin guide](https://github.com/intertwine/grok-observational-memory#uninstall).
  Remove its stale enabled-plugin name if the host leaves one. Core-managed Grok
  hooks and the standalone plugin are distinct installations.
- **OpenClaw skill:** remove only its observer/reflector jobs, its installed skill,
  and the OM-specific startup instructions. Preserve native memory and unrelated
  jobs. Verify current scheduler identities instead of relying on old job files.
- **Mail/Cluster:** stop OM-owned listeners, relays, token refreshers and sync jobs
  only after checking consumers. Preserve shared services and accounts. Archive
  OM-only configuration securely; revoke dedicated credentials only when ownership
  and remaining consumers are known.
- **QMD:** optionally remove the exact OM collection from each relevant index.
  Keep QMD, unrelated collections, and source Markdown. Index entries are derived
  data; the backup must preserve the source documents.

Remove each discovered runtime package with its original package manager. For
example, choose the applicable command, not all commands blindly:

```sh
uv tool uninstall observational-memory
HOMEBREW_NO_AUTOREMOVE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew uninstall observational-memory
```

For a package installed inside an agent's Python environment, remove only the OM
package from that exact environment; do not delete the whole environment. Keep
unrelated dependencies. Restart affected agent sessions and check that OM tools,
hooks and services no longer load or restart. Verify required native instructions
and memory controls still work. Missing OM services are expected after retirement,
not a reason to reinstall them because an old `om doctor` check recommends it.

## Compatibility and limits

Core 0.10.1 retains the 0.10 API family and does not remotely disable existing
installations. Companion closeouts are Hermes 1.5.2 and Grok 0.1.3. These are
legacy artifacts, not promises of compatibility with future host releases.

The closeout uses deterministic fixture tests, including shared-hook preservation,
malformed settings, scoped startup failure, text-only provider extraction and
truncation rejection. Release notes record the actual tested runtime versions.
Mocked provider tests do not establish a successful paid provider call or hosted
memory import. No future security response or migration service is committed.
