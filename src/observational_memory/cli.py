"""CLI entry points: om observe, om reflect, om install, om status."""

from __future__ import annotations

from pathlib import Path

import click

from .config import Config


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Observational Memory — cross-agent shared memory for Claude Code & Codex CLI."""
    ctx.ensure_object(dict)
    config = Config()
    config.load_env_file()
    ctx.obj["config"] = config


@cli.command()
@click.option("--transcript", type=click.Path(exists=True, path_type=Path), help="Specific transcript file to process")
@click.option("--source", type=click.Choice(["claude", "codex", "all"]), default="all", help="Which agent transcripts to process")
@click.option("--dry-run", is_flag=True, help="Print observations without writing")
@click.pass_context
def observe(ctx: click.Context, transcript: Path | None, source: str, dry_run: bool) -> None:
    """Run the observer to compress transcripts into observations."""
    from .observe import observe_all_claude, observe_all_codex, observe_claude_transcript

    config = ctx.obj["config"]

    if transcript:
        click.echo(f"Processing transcript: {transcript}")
        result = observe_claude_transcript(transcript, config, dry_run)
        if result:
            click.echo(f"Observations updated ({len(result)} chars)")
            if dry_run:
                click.echo(result)
        else:
            click.echo("No new messages to process.")
        return

    results = []
    if source in ("claude", "all"):
        click.echo("Scanning Claude Code transcripts...")
        results.extend(observe_all_claude(config, dry_run))

    if source in ("codex", "all"):
        click.echo("Scanning Codex sessions...")
        results.extend(observe_all_codex(config, dry_run))

    if results:
        click.echo(f"Processed {len(results)} transcript(s)")
        if dry_run:
            for r in results:
                click.echo("---")
                click.echo(r)
    else:
        click.echo("No new messages to process.")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print reflections without writing")
@click.pass_context
def reflect(ctx: click.Context, dry_run: bool) -> None:
    """Run the reflector to condense observations into long-term memory."""
    from .reflect import run_reflector

    config = ctx.obj["config"]
    click.echo("Running reflector...")

    result = run_reflector(config, dry_run)
    if result:
        click.echo(f"Reflections updated ({len(result)} chars)")
        if dry_run:
            click.echo(result)
    else:
        click.echo("No observations to reflect on.")


@cli.command()
@click.option("--claude", "targets", flag_value="claude", help="Install Claude Code hooks")
@click.option("--codex", "targets", flag_value="codex", help="Install Codex AGENTS.md additions")
@click.option("--both", "targets", flag_value="both", default=True, help="Install both (default)")
@click.option("--cron/--no-cron", default=True, help="Set up cron jobs")
@click.pass_context
def install(ctx: click.Context, targets: str, cron: bool) -> None:
    """Set up observational memory for Claude Code and/or Codex CLI."""
    config = ctx.obj["config"]
    config.ensure_memory_dir()

    # Create env file for API keys
    if config.ensure_env_file():
        click.echo(f"Created {config.env_file}")
        click.echo(f"  Add your API key: {config.env_file}")
    else:
        click.echo(f"Env file: {config.env_file} (already exists)")

    # Create initial memory files
    if not config.observations_path.exists():
        config.observations_path.write_text("# Observations\n\n<!-- Auto-maintained by the Observer. -->\n")
        click.echo(f"Created {config.observations_path}")

    if not config.reflections_path.exists():
        config.reflections_path.write_text(
            "# Reflections — Long-Term Memory\n\n"
            "*Last updated: never*\n\n"
            "<!-- Auto-maintained by the Reflector. -->\n\n"
            "## Core Identity\n\n"
            "## Active Projects\n\n"
            "## Preferences & Opinions\n\n"
            "## Relationship & Communication\n\n"
            "## Key Facts & Context\n\n"
            "## Recent Themes\n\n"
            "## Archive\n"
        )
        click.echo(f"Created {config.reflections_path}")

    if targets in ("claude", "both"):
        _install_claude_hooks(config)

    if targets in ("codex", "both"):
        _install_codex(config)

    if cron:
        _install_cron(config, targets)

    click.echo("\nInstallation complete! Run 'om status' to verify.")


@cli.command()
@click.option("--claude", "targets", flag_value="claude")
@click.option("--codex", "targets", flag_value="codex")
@click.option("--both", "targets", flag_value="both", default=True)
@click.option("--purge", is_flag=True, help="Also remove memory files")
@click.pass_context
def uninstall(ctx: click.Context, targets: str, purge: bool) -> None:
    """Remove observational memory hooks and cron jobs."""
    config = ctx.obj["config"]

    if targets in ("claude", "both"):
        _uninstall_claude_hooks(config)

    if targets in ("codex", "both"):
        _uninstall_codex(config)

    _uninstall_cron()

    if purge:
        import shutil
        if config.memory_dir.exists():
            shutil.rmtree(config.memory_dir)
            click.echo(f"Removed {config.memory_dir}")

    click.echo("Uninstall complete.")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show status of observational memory files and configuration."""
    config = ctx.obj["config"]

    click.echo("Observational Memory Status")
    click.echo("=" * 40)

    # Memory directory
    click.echo(f"\nMemory dir: {config.memory_dir}")
    click.echo(f"  Exists: {config.memory_dir.exists()}")

    # Observations
    if config.observations_path.exists():
        obs = config.observations_path.read_text()
        lines = len(obs.splitlines())
        size = len(obs)
        click.echo(f"\nObservations: {config.observations_path}")
        click.echo(f"  Lines: {lines}, Size: {size} bytes")
    else:
        click.echo("\nObservations: not created yet")

    # Reflections
    if config.reflections_path.exists():
        ref = config.reflections_path.read_text()
        lines = len(ref.splitlines())
        size = len(ref)
        click.echo(f"\nReflections: {config.reflections_path}")
        click.echo(f"  Lines: {lines}, Size: {size} bytes")
    else:
        click.echo("\nReflections: not created yet")

    # Cursor
    cursor = config.load_cursor()
    if cursor:
        click.echo(f"\nCursor: tracking {len(cursor)} transcript(s)")
    else:
        click.echo("\nCursor: no transcripts tracked yet")

    # Env file
    click.echo(f"\nEnv file: {config.env_file}")
    if config.env_file.exists():
        # Count non-comment, non-empty lines (i.e. actual key assignments)
        env_lines = [
            l.strip() for l in config.env_file.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        click.echo(f"  Exists: yes ({len(env_lines)} key(s) configured)")
    else:
        click.echo("  Exists: no (run 'om install' to create)")

    # API keys (from env file + environment)
    import os
    click.echo(f"\nAnthropic API key: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    click.echo(f"OpenAI API key: {'set' if os.environ.get('OPENAI_API_KEY') else 'not set'}")

    # Claude Code hooks
    if config.claude_settings_path.exists():
        import json
        settings = json.loads(config.claude_settings_path.read_text())
        hooks = settings.get("hooks", {})
        has_start = "SessionStart" in hooks
        has_end = "SessionEnd" in hooks
        click.echo(f"\nClaude Code hooks:")
        click.echo(f"  SessionStart: {'installed' if has_start else 'not installed'}")
        click.echo(f"  SessionEnd: {'installed' if has_end else 'not installed'}")
    else:
        click.echo(f"\nClaude Code: settings not found at {config.claude_settings_path}")

    # Codex AGENTS.md
    if config.codex_agents_md.exists():
        content = config.codex_agents_md.read_text()
        has_om = "observational-memory" in content.lower()
        click.echo(f"\nCodex AGENTS.md: {'contains OM instructions' if has_om else 'no OM instructions'}")
    else:
        click.echo(f"\nCodex: AGENTS.md not found at {config.codex_agents_md}")


# --- Claude Code hook installation ---

def _install_claude_hooks(config: Config) -> None:
    """Add SessionStart and SessionEnd hooks to ~/.claude/settings.json."""
    import json

    hooks_dir = Path(__file__).parent / "hooks" / "claude"
    session_start_hook = hooks_dir / "session-start.sh"
    session_end_hook = hooks_dir / "session-end.sh"

    if not config.claude_settings_path.exists():
        config.claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}
    else:
        settings = json.loads(config.claude_settings_path.read_text())

    hooks = settings.setdefault("hooks", {})

    # SessionStart hook
    hooks["SessionStart"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": str(session_start_hook),
                    "timeout": 5,
                    "statusMessage": "Loading observational memory..."
                }
            ]
        }
    ]

    # SessionEnd hook
    hooks["SessionEnd"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": str(session_end_hook),
                    "timeout": 60,
                    "async": True
                }
            ]
        }
    ]

    config.claude_settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    click.echo("Installed Claude Code hooks (SessionStart + SessionEnd)")


def _uninstall_claude_hooks(config: Config) -> None:
    """Remove observational memory hooks from Claude Code settings."""
    import json

    if not config.claude_settings_path.exists():
        return

    settings = json.loads(config.claude_settings_path.read_text())
    hooks = settings.get("hooks", {})
    hooks.pop("SessionStart", None)
    hooks.pop("SessionEnd", None)
    if not hooks:
        settings.pop("hooks", None)

    config.claude_settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    click.echo("Removed Claude Code hooks")


# --- Codex integration ---

_CODEX_OM_MARKER = "<!-- observational-memory -->"

_CODEX_OM_BLOCK = f"""{_CODEX_OM_MARKER}
## Observational Memory

At the start of every session, read these files for context about the user:

1. `~/.local/share/observational-memory/reflections.md` — long-term memory (identity, projects, preferences)
2. `~/.local/share/observational-memory/observations.md` — recent compressed observations

These files are auto-maintained. Do not modify them directly.
{_CODEX_OM_MARKER}"""


def _install_codex(config: Config) -> None:
    """Add observational memory instructions to ~/.codex/AGENTS.md."""
    agents_md = config.codex_agents_md

    if agents_md.exists():
        content = agents_md.read_text()
        if _CODEX_OM_MARKER in content:
            click.echo("Codex AGENTS.md already has observational memory instructions")
            return
        content = content.rstrip() + "\n\n" + _CODEX_OM_BLOCK + "\n"
    else:
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        content = _CODEX_OM_BLOCK + "\n"

    agents_md.write_text(content)
    click.echo(f"Added observational memory instructions to {agents_md}")


def _uninstall_codex(config: Config) -> None:
    """Remove observational memory block from Codex AGENTS.md."""
    agents_md = config.codex_agents_md
    if not agents_md.exists():
        return

    content = agents_md.read_text()
    if _CODEX_OM_MARKER not in content:
        return

    # Remove the OM block
    import re
    pattern = rf"\n*{re.escape(_CODEX_OM_MARKER)}.*?{re.escape(_CODEX_OM_MARKER)}\n*"
    content = re.sub(pattern, "\n", content, flags=re.DOTALL)
    agents_md.write_text(content.strip() + "\n" if content.strip() else "")
    click.echo("Removed observational memory from Codex AGENTS.md")


# --- Cron installation ---

def _install_cron(config: Config, targets: str) -> None:
    """Add cron jobs for observer and reflector."""
    import subprocess

    om_path = _find_om_path()
    if not om_path:
        click.echo("Warning: 'om' not found in PATH. Cron jobs will use 'om' — make sure it's installed.")
        om_path = "om"

    # Source the env file before each cron command so API keys are available
    env_file = config.env_file
    if env_file.exists():
        prefix = f". {env_file} && "
    else:
        prefix = ""

    jobs = []

    if targets in ("codex", "both"):
        # Observer cron for Codex (Claude uses hooks instead)
        jobs.append(f"*/15 * * * * {prefix}{om_path} observe --source codex 2>/dev/null")

    # Daily reflector for all
    jobs.append(f"0 4 * * * {prefix}{om_path} reflect 2>/dev/null")

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        existing = ""

    # Remove old OM cron lines
    lines = [l for l in existing.splitlines() if "om observe" not in l and "om reflect" not in l]

    lines.append("# --- observational-memory ---")
    lines.extend(jobs)
    lines.append("# --- end observational-memory ---")

    new_crontab = "\n".join(lines) + "\n"

    proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if proc.returncode == 0:
        click.echo(f"Installed {len(jobs)} cron job(s)")
    else:
        click.echo(f"Warning: Failed to install cron jobs: {proc.stderr}")


def _uninstall_cron() -> None:
    """Remove observational memory cron jobs."""
    import subprocess

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return
    except FileNotFoundError:
        return

    lines = result.stdout.splitlines()
    filtered = []
    in_om_block = False
    for line in lines:
        if "--- observational-memory ---" in line:
            in_om_block = not in_om_block
            continue
        if in_om_block:
            continue
        # Also remove loose OM lines
        if "om observe" in line or "om reflect" in line:
            continue
        filtered.append(line)

    new_crontab = "\n".join(filtered) + "\n" if filtered else ""
    subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    click.echo("Removed cron jobs")


def _find_om_path() -> str | None:
    """Find the absolute path to the 'om' command."""
    import shutil
    return shutil.which("om")
