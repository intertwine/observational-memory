# Install And Setup

> Unmaintained legacy software as of September 5, 2026. No updates or security
> fixes are promised. These instructions are historical; see
> [legacy status and migration](legacy-migration.md) before use.

This guide is for people installing Observational Memory for day-to-day use. Maintainer and release commands live in [MAINTAINERS.md](MAINTAINERS.md).

## What You Need

- Homebrew or `uv`
- For the v0.10.0 native-memory bridge: macOS and at least one eligible Claude project memory directory. Fixed Codex memory files are included when present.
- For the full OM observation and reflection workflow: Python 3.11 or newer, a supported agent, and one LLM provider:
  - Anthropic API key
  - OpenAI API key
  - **OpenAI ChatGPT subscription** (`om login openai-chatgpt`)
  - **xAI Grok / SuperGrok subscription** (`om login xai-oauth`)
  - xAI API key
  - Anthropic on Vertex AI
  - Anthropic on Bedrock

The native-memory bridge does not need a provider or login. It reads only approved native memory summaries and builds a local BM25 index.

If you enable the full workflow and already pay for ChatGPT Plus / Pro / Team or SuperGrok, prefer `om login` over an API key — it routes calls through your subscription instead of charging per token. See [configuration.md](configuration.md) for the cost comparison and how the auth flows work.

## Fast Native-Bridge Install

For a new Homebrew install:

```bash
brew install intertwine/tap/observational-memory
```

If OM is already installed with Homebrew:

```bash
brew upgrade observational-memory
```

For a new `uv` install:

```bash
uv tool install observational-memory
```

If OM is already installed with `uv`:

```bash
uv tool upgrade observational-memory
```

Then choose the native sources and enable the bridge:

```bash
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

If Claude Code or Codex was running during installation, save the current work, exit the affected app or CLI session, and start a new session. A running process can retain its previous OM write hooks until it restarts.

Build the first index and check the result:

```bash
om bridge-native-memory
om search --native-bridge "current project status"
om native-bridge status
om doctor
```

`om native-bridge sources` lists eligible Claude project directory names with Markdown-file counts and reports whether the two fixed Codex source files are present. It does not show memory text or enroll anything. The first install requires at least one exact Claude project directory name from that list. Repeat `--claude-project` for more projects.

Setup is ready when status shows `Config: configured`, `LaunchAgent: enabled and loaded`, and `Generation: ready`, and `om doctor` reports that the older Claude and Codex writers are inactive.

Flags on `om install --native-bridge` replace the complete saved Claude selection. To add or remove a project, repeat every project you want to keep:

```bash
om install --native-bridge \
  --claude-project "<first-project>" \
  --claude-project "<second-project>"
```

Flags on `om bridge-native-memory` apply only to that one refresh. They do not change the saved selection, so the next scheduled run returns to the projects saved by `om install --native-bridge`.

The bridge service checks for changed Claude Code and Codex memory summaries every 15 minutes. The one-shot command runs the same bounded path immediately. It does not read raw transcripts, call an LLM, or run reflection. Search its isolated index explicitly:

```bash
om search --native-bridge "current project status"
```

Ordinary `om search` and `om recall` keep using the full OM memory store. They do not merge bridge results.

See [Native Claude and Codex memory](native-memory-bridge.md) for source scope, retrieval, upgrade, rollback, and troubleshooting.

## Full OM Install

Use the full installer when you want OM to observe supported agent sessions and maintain its own Markdown memory. On a fresh install:

```bash
om install
om doctor
```

If the native bridge is already enabled, switch modes first so both schedulers are not left active:

```bash
om uninstall --native-bridge
om install --both
```

If you previously used Cowork writers, also run `om install --cowork`. `--both` restores Claude Code and Codex only.

Save current work in Claude Code or Codex, exit the affected app or CLI session, and start a new session so it loads the restored hooks. Then run `om doctor`.

Enterprise auth extras:

```bash
uv tool install "observational-memory[enterprise]"
```

## First Full-Workflow Run

Run the installer:

```bash
om install
```

The installer sets up:

- local config in `~/.config/observational-memory/env`
- memory files in `~/.local/share/observational-memory/`
- Claude Code hooks when requested
- Codex hooks and the AGENTS fallback when requested
- OpenCode plugin, Kimi hooks, and Grok hooks when requested
- background observer and reflector jobs

Then check the install:

```bash
om status
om doctor
```

Use `om doctor --validate-key` when you want to confirm the configured LLM provider can make a live call.

`--validate-key` is not needed for a bridge-only install because the bridge does not use an LLM provider.

## Non-Interactive Install

Use this in scripts or remote setup:

```bash
om install \
  --provider anthropic \
  --llm-model claude-sonnet-4-5-20250929 \
  --non-interactive
```

The provider key still comes from your environment or the private env file.

Vertex AI:

```bash
om install \
  --provider anthropic-vertex \
  --vertex-project-id my-project \
  --vertex-region us-east5 \
  --llm-model claude-sonnet-4-5-20250929 \
  --non-interactive
```

Bedrock:

```bash
om install \
  --provider anthropic-bedrock \
  --bedrock-region us-east-1 \
  --llm-model anthropic.claude-sonnet-4-5-20250929-v1:0 \
  --non-interactive
```

## Choose Integrations

```bash
om install --claude
om install --codex
om install --grok
om install --opencode
om install --kimi
om install --both
om install --cowork
om install --all
```

`--both` installs Claude Code and Codex support. `--all` also installs OpenCode, Kimi, Grok support, and tries Cowork. Cowork is macOS-only.

## Scheduler Choices

```bash
om install --scheduler auto
om install --scheduler launchd
om install --scheduler cron
om install --scheduler schtasks
om install --scheduler none
```

Defaults:

- macOS: launchd
- Linux: cron
- Windows: Task Scheduler

Use `--scheduler none` if you want to run `om observe` and `om reflect` yourself.

These scheduler flags apply to the full transcript-based workflow. The native bridge has its own fixed 15-minute macOS service; `om install --native-bridge` manages it directly.

## Upgrade From v0.9.1

Upgrade the package, then choose the bridge explicitly:

```bash
brew upgrade observational-memory   # or: uv tool upgrade observational-memory
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
```

If Claude Code or Codex was running during installation, save the current work, exit the affected app or CLI session, and start a new session. Then build and check the index:

```bash
om bridge-native-memory
om search --native-bridge "current project status"
om native-bridge status
om doctor
```

On an existing full install, `om install --native-bridge` boots out the four older macOS writer jobs: the Claude observer, Codex observer, Claude auto-memory scan, and reflector. Their plist files stay in place, but the bridge installer does not enable them.

It also removes OM-managed Codex Stop, Claude checkpoint, and Cowork writer hooks. It preserves OM's read-only SessionStart context hooks and fallback, along with unrelated hook groups. If bridge setup fails, the installer restores the managed files and each legacy service that was explicitly enabled before the attempt. A service whose prior launchd state was `default` or could not be determined stays disabled for safety and appears in `om doctor`. The installer reports `rollback incomplete` only if a restoration step itself fails.

Grok, Kimi, and OpenCode are outside this Claude↔Codex migration. If you installed their writer integrations earlier, they are unchanged and may still feed the full OM workflow.

This switch does not delete OM Markdown memory or the native Claude Code and Codex source files.

## Windows Notes

On Windows:

- memory lives under `%LOCALAPPDATA%\observational-memory\`
- config lives under `%APPDATA%\observational-memory\`
- scheduled jobs use Task Scheduler
- Claude hooks call `om` directly, so `bash` and `jq` are not required
- Grok hooks call `om` directly, so `bash` and `jq` are not required
- Cowork install is skipped because Cowork is macOS-only
- the v0.10.0 native-memory bridge is unavailable; `om install --native-bridge` fails without changing the install

PowerShell examples:

```powershell
uv tool install observational-memory
om install --scheduler schtasks
om doctor
```

## Uninstall

Temporarily disable scheduled refresh while keeping the service definition:

```bash
om native-bridge disable
```

Re-enable it with `om install --native-bridge`; the installer reuses the saved Claude project selection.

Disable and remove only the native-memory bridge service:

```bash
om uninstall --native-bridge
```

Both paths leave Claude Code and Codex native memory untouched. Private bridge config, derived index data, and receipts are also preserved for diagnosis or re-enabling. Uninstall is the feature-level rollback path if you want to keep OM v0.10.0 but remove the scheduled service.

Remove the service and all bridge-derived local state while keeping the source memories untouched:

```bash
om uninstall --native-bridge --purge
```

This command first boots out the exact bridge service and verifies that it is absent. It then removes only the private bridge selection, derived generations, receipts, and bridge log files. It does not delete Claude Code or Codex native memory.

Because purge removes the saved selection, re-enable the bridge with a new explicit selection:

```bash
om native-bridge sources
om install --native-bridge --claude-project "<exact-directory-name-from-the-list>"
om bridge-native-memory
```

Removing the bridge does not restart older writer jobs. To leave the bridge and explicitly return to the full Claude Code and Codex workflow:

```bash
om uninstall --native-bridge
om install --both
om install --cowork   # only if you want to restore Cowork writers too
```

`--both` restores Claude Code and Codex only. Save current work, exit the affected Claude Code or Codex app or CLI session, and start a new session so it loads the restored hooks. Then run `om doctor`.

Remove hooks and scheduled jobs:

```bash
om uninstall
```

Remove memory files too:

```bash
om uninstall --purge
```

Use `--purge` carefully. `om uninstall --purge` deletes the full OM memory directory, but it is not a substitute for removing the separate bridge service and config. Run `om uninstall --native-bridge --purge` first when the bridge is installed.
