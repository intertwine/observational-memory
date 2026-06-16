# Install And Setup

This guide is for people installing Observational Memory for day-to-day use. Maintainer and release commands live in [MAINTAINERS.md](MAINTAINERS.md).

## What You Need

- Python 3.11 or newer
- `uv` or Homebrew
- Claude Code, Codex, OpenCode, and/or Grok Build TUI if you want automatic hooks
- One LLM provider:
  - Anthropic API key
  - OpenAI API key
  - **OpenAI ChatGPT subscription** (`om login openai-chatgpt`)
  - **xAI Grok / SuperGrok subscription** (`om login xai-oauth`)
  - xAI API key
  - Anthropic on Vertex AI
  - Anthropic on Bedrock

If you already pay for ChatGPT Plus / Pro / Team or SuperGrok, prefer `om login` over an API key — it routes calls through your subscription instead of charging per token. See [configuration.md](configuration.md) for the cost comparison and how the auth flows work.

## Fast Install

macOS with Homebrew:

```bash
brew install intertwine/tap/observational-memory
om install
om doctor
```

Any platform with `uv`:

```bash
uv tool install observational-memory
om install
om doctor
```

Enterprise auth extras:

```bash
uv tool install "observational-memory[enterprise]"
```

## First Run

Run the installer:

```bash
om install
```

The installer sets up:

- local config in `~/.config/observational-memory/env`
- memory files in `~/.local/share/observational-memory/`
- Claude Code hooks when requested
- Codex hooks and the AGENTS fallback when requested
- OpenCode plugin and Grok hooks when requested
- background observer and reflector jobs

Then check the install:

```bash
om status
om doctor
```

Use `om doctor --validate-key` when you want to confirm the configured LLM provider can make a live call.

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
om install --both
om install --cowork
om install --all
```

`--both` installs Claude Code and Codex support. `--all` also installs OpenCode, Grok support, and tries Cowork. Cowork is macOS-only.

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

## Windows Notes

On Windows:

- memory lives under `%LOCALAPPDATA%\observational-memory\`
- config lives under `%APPDATA%\observational-memory\`
- scheduled jobs use Task Scheduler
- Claude hooks call `om` directly, so `bash` and `jq` are not required
- Grok hooks call `om` directly, so `bash` and `jq` are not required
- Cowork install is skipped because Cowork is macOS-only

PowerShell examples:

```powershell
uv tool install observational-memory
om install --scheduler schtasks
om doctor
```

## Uninstall

Remove hooks and scheduled jobs:

```bash
om uninstall
```

Remove memory files too:

```bash
om uninstall --purge
```

Use `--purge` carefully. It deletes local OM data.
