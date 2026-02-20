"""CLI entry points: om observe, om reflect, om backfill, om search, om context, om install, om status, om doctor."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click

from .config import Config


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Observational Memory — cross-agent shared memory for Claude Code & Codex CLI."""
    ctx.ensure_object(dict)
    Config().load_env_file()  # Seed os.environ before constructing final config
    config = Config()
    ctx.obj["config"] = config


@cli.command()
@click.option("--transcript", type=click.Path(exists=True, path_type=Path), help="Specific transcript file to process")
@click.option(
    "--source", type=click.Choice(["claude", "codex", "all"]), default="all", help="Which agent transcripts to process"
)
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
@click.option(
    "--source", type=click.Choice(["claude", "codex", "all"]), default="all", help="Which transcripts to process"
)
@click.option("--dry-run", is_flag=True, help="Show what would be processed without writing")
@click.option("--limit", type=int, default=0, help="Max transcripts to process (0 = unlimited)")
@click.option("--reflect-every", type=int, default=20, help="Run reflector every N transcripts")
@click.option("--chunk-size", type=int, default=200, help="Max messages per LLM call")
@click.pass_context
def backfill(ctx: click.Context, source: str, dry_run: bool, limit: int, reflect_every: int, chunk_size: int) -> None:
    """Process all historical transcripts through the observer and reflector.

    Discovers all existing transcripts, processes them oldest-first through
    the observer (in backfill mode — lightweight context), and periodically
    runs the reflector to condense observations.

    Idempotent: already-processed transcripts are skipped via the cursor.
    Safe to interrupt and resume.
    """
    from .observe import observe_claude_transcript_backfill
    from .reflect import run_reflector
    from .transcripts.claude import find_all_transcripts
    from .transcripts.codex import find_recent_sessions

    config = ctx.obj["config"]

    # Discover transcripts
    all_transcripts: list[tuple[Path, str]] = []  # (path, source_label)

    if source in ("claude", "all"):
        for p in find_all_transcripts(config.claude_projects_dir):
            all_transcripts.append((p, "claude"))

    if source in ("codex", "all"):
        for p in find_recent_sessions(config.codex_home):
            all_transcripts.append((p, "codex"))

    if not all_transcripts:
        click.echo("No transcripts found.")
        return

    # Filter out already-processed transcripts for display count
    cursor = config.load_cursor()
    unprocessed = [(p, s) for p, s in all_transcripts if str(p) not in cursor]
    total = len(all_transcripts)
    pending = len(unprocessed)

    click.echo(f"Found {total} transcript(s), {pending} unprocessed")

    if dry_run:
        for p, s in unprocessed[: limit or None]:
            size = p.stat().st_size
            project = p.parent.name
            click.echo(f"  [{s}] {project}/{p.name} ({size:,} bytes)")
        return

    if pending == 0:
        click.echo("All transcripts already processed. Nothing to do.")
        return

    # Process transcripts
    processed = 0
    errors = 0
    total_chars = 0

    for path, src in all_transcripts:
        if limit and processed >= limit:
            click.echo(f"\nReached limit of {limit} transcripts.")
            break

        # Skip already-processed (cursor check is also inside observe_*_backfill,
        # but checking here avoids noisy output)
        if str(path) in cursor:
            continue

        project = path.parent.name
        processed += 1
        click.echo(f"[{processed}/{pending}] {project}/{path.name[:12]}... ", nl=False)

        try:
            if src == "claude":
                chars = observe_claude_transcript_backfill(path, config, chunk_size)
            else:
                # Codex backfill uses the same approach via observe_all_codex
                # For now, process Claude transcripts (Codex support can be added)
                chars = None

            if chars:
                total_chars += chars
                click.echo(f"({chars:,} chars)")
            else:
                click.echo("(no new messages)")
        except Exception as e:
            errors += 1
            click.echo(f"ERROR: {e}")

        # Periodic reflector
        if reflect_every and processed % reflect_every == 0:
            click.echo(f"\n--- Running reflector (every {reflect_every} transcripts) ---")
            try:
                run_reflector(config)
                click.echo("--- Reflections updated ---\n")
            except Exception as e:
                click.echo(f"--- Reflector error: {e} ---\n")

        # Reload cursor after each transcript (it may have been updated)
        cursor = config.load_cursor()

    # Final reflector run
    if processed > 0:
        click.echo("\n--- Final reflector run ---")
        try:
            result = run_reflector(config)
            if result:
                click.echo(f"Reflections updated ({len(result):,} chars)")
            else:
                click.echo("No observations to reflect on.")
        except Exception as e:
            click.echo(f"Reflector error: {e}")

    click.echo(
        f"\nBackfill complete: {processed} transcript(s), {total_chars:,} chars of observations, {errors} error(s)"
    )


@cli.command()
@click.argument("query")
@click.option("--limit", "-n", type=int, default=10, help="Max results to return")
@click.option("--reindex", is_flag=True, help="Rebuild the search index before searching")
@click.option("--json", "as_json", is_flag=True, help="Output results as JSON")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, reindex: bool, as_json: bool) -> None:
    """Search observations and reflections for relevant memories."""
    from .search import get_backend
    from .search import reindex as do_reindex

    config = ctx.obj["config"]

    if reindex:
        n = do_reindex(config)
        if not as_json:
            click.echo(f"Indexed {n} document(s)")

    backend = get_backend(config.search_backend, config)

    if not backend.is_ready():
        # Auto-index on first search
        n = do_reindex(config)
        if not as_json:
            click.echo(f"Built index ({n} document(s))")

    results = backend.search(query, limit=limit)

    if as_json:
        import json as json_mod

        output = [
            {
                "rank": r.rank,
                "score": r.score,
                "doc_id": r.document.doc_id,
                "source": r.document.source.value,
                "heading": r.document.heading,
                "content": r.document.content[:500],
            }
            for r in results
        ]
        click.echo(json_mod.dumps(output, indent=2))
    elif results:
        for r in results:
            click.echo(f"\n--- [{r.rank}] {r.document.heading} (score: {r.score:.2f}) ---")
            # Show first 5 lines of content
            lines = r.document.content.strip().splitlines()
            for line in lines[:5]:
                click.echo(f"  {line}")
            if len(lines) > 5:
                click.echo(f"  ... ({len(lines) - 5} more lines)")
    else:
        click.echo("No results found.")


@cli.command(hidden=True)
@click.pass_context
def context(ctx: click.Context) -> None:
    """Generate session-start JSON with search-backed memory retrieval.

    Called by the SessionStart hook. Outputs JSON with additionalContext
    containing full reflections + search results (or full observations as fallback).
    """
    import json as json_mod

    from .search import get_backend

    config = ctx.obj["config"]
    parts = []

    # Always include full reflections (small by design, 200-600 lines)
    if config.reflections_path.exists() and config.reflections_path.stat().st_size > 0:
        parts.append("## Long-Term Memory (Reflections)\n\n" + config.reflections_path.read_text())

    # Try search-based observation retrieval
    observations_added = False
    backend = get_backend(config.search_backend, config)
    if backend.is_ready():
        # Search for recent context — use a broad query
        results = backend.search("recent context current tasks projects", limit=10)
        if results:
            obs_parts = []
            for r in results:
                if r.document.source.value == "observations":
                    obs_parts.append(r.document.content)
            if obs_parts:
                parts.append("## Recent Observations\n\n" + "\n\n".join(obs_parts))
                observations_added = True

    # Fallback: include full observations file
    if not observations_added:
        if config.observations_path.exists() and config.observations_path.stat().st_size > 0:
            parts.append("## Recent Observations\n\n" + config.observations_path.read_text())

    if parts:
        context_text = "\n\n---\n\n".join(parts)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context_text,
            }
        }
        click.echo(json_mod.dumps(output))


def _has_api_key() -> bool:
    """Check if any LLM API key is configured (env file or environment)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def _validate_api_key_format(key: str, provider: str) -> bool:
    """Basic format validation for API keys."""
    if provider == "anthropic":
        return key.startswith("sk-ant-") and len(key) > 20
    elif provider == "openai":
        return key.startswith("sk-") and len(key) > 20
    return False


def _prompt_api_key(config: Config) -> None:
    """Interactively prompt for an API key if none is configured."""
    if _has_api_key():
        return

    if not sys.stdin.isatty():
        click.echo("Warning: No API key configured and stdin is not a TTY. Skipping API key setup.")
        click.echo(f"  Add your key manually to: {config.env_file}")
        return

    click.echo("\nNo API key found. Let's set one up.")
    provider = click.prompt(
        "Which provider?",
        type=click.Choice(["anthropic", "openai"], case_sensitive=False),
        default="anthropic",
    )

    key = click.prompt(f"Paste your {provider} API key", hide_input=True)

    if not key.strip():
        click.echo("Empty key — skipping.")
        return

    key = key.strip()

    # Validate format
    if not _validate_api_key_format(key, provider):
        click.echo(f"Warning: Key doesn't match expected {provider} format, but saving anyway.")

    # Try a live validation
    validated = False
    try:
        if provider == "anthropic":
            from anthropic import Anthropic

            client = Anthropic(api_key=key)
            msg = [{"role": "user", "content": "hi"}]
            client.messages.create(model="claude-sonnet-4-5-20250929", max_tokens=1, messages=msg)
            validated = True
        elif provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=key)
            msg = [{"role": "user", "content": "hi"}]
            client.chat.completions.create(model="gpt-4o-mini", max_tokens=1, messages=msg)
            validated = True
    except Exception:
        click.echo("Could not validate key via API (network issue?) — saving based on format check.")

    if validated:
        click.echo("API key validated successfully.")

    # Write to env file
    env_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    env_file = config.env_file
    if env_file.exists():
        content = env_file.read_text()
        # Replace commented-out line or append
        if f"# {env_var}=" in content:
            content = content.replace(f"# {env_var}=sk-ant-...", f"{env_var}={key}")
            content = content.replace(f"# {env_var}=sk-...", f"{env_var}={key}")
        else:
            content = content.rstrip() + f"\n{env_var}={key}\n"
        env_file.write_text(content)
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"{env_var}={key}\n")
        env_file.chmod(0o600)

    # Also set in current process so subsequent steps see it
    os.environ[env_var] = key
    click.echo(f"Saved to {env_file}")


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

    # Prompt for API key if none configured
    _prompt_api_key(config)

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
            line.strip()
            for line in config.env_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
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
        has_prompt_submit = "UserPromptSubmit" in hooks
        has_precompact = "PreCompact" in hooks
        click.echo("\nClaude Code hooks:")
        click.echo(f"  SessionStart: {'installed' if has_start else 'not installed'}")
        click.echo(f"  SessionEnd: {'installed' if has_end else 'not installed'}")
        click.echo(f"  UserPromptSubmit: {'installed' if has_prompt_submit else 'not installed'}")
        click.echo(f"  PreCompact: {'installed' if has_precompact else 'not installed'}")
    else:
        click.echo(f"\nClaude Code: settings not found at {config.claude_settings_path}")

    # Codex AGENTS.md
    if config.codex_agents_md.exists():
        content = config.codex_agents_md.read_text()
        has_om = "observational-memory" in content.lower()
        click.echo(f"\nCodex AGENTS.md: {'contains OM instructions' if has_om else 'no OM instructions'}")
    else:
        click.echo(f"\nCodex: AGENTS.md not found at {config.codex_agents_md}")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--validate-key", is_flag=True, help="Test API key with a live API call")
@click.pass_context
def doctor(ctx: click.Context, as_json: bool, validate_key: bool) -> None:
    """Run diagnostic checks on your observational memory installation."""
    import json as json_mod
    import subprocess

    config = ctx.obj["config"]
    results: list[dict] = []

    def _check(name: str, status: str, detail: str, fix: str = "") -> None:
        results.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 1. Python version
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ver >= (3, 11):
        _check("Python version", "PASS", ver_str)
    else:
        _check("Python version", "FAIL", ver_str, fix="Upgrade to Python 3.11+")

    # 2. om binary on PATH
    om_path = shutil.which("om")
    if om_path:
        _check("om binary", "PASS", om_path)
    else:
        _check("om binary", "FAIL", "not found on PATH", fix="Run: uv tool install observational-memory")

    # 3. API key present
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if anthropic_key or openai_key:
        providers = []
        if anthropic_key:
            providers.append("anthropic")
        if openai_key:
            providers.append("openai")
        _check("API key present", "PASS", ", ".join(providers))
    else:
        _check(
            "API key present",
            "FAIL",
            "neither ANTHROPIC_API_KEY nor OPENAI_API_KEY set",
            fix=f"Add your key to {config.env_file}",
        )

    # 4. API key valid (opt-in)
    if validate_key:
        if anthropic_key:
            try:
                from anthropic import Anthropic

                client = Anthropic(api_key=anthropic_key)
                msg = [{"role": "user", "content": "hi"}]
                client.messages.create(model="claude-sonnet-4-5-20250929", max_tokens=1, messages=msg)
                _check("API key valid", "PASS", "anthropic key works")
            except Exception as e:
                _check("API key valid", "FAIL", f"anthropic: {e}", fix="Check your ANTHROPIC_API_KEY")
        elif openai_key:
            try:
                from openai import OpenAI

                client = OpenAI(api_key=openai_key)
                msg = [{"role": "user", "content": "hi"}]
                client.chat.completions.create(model="gpt-4o-mini", max_tokens=1, messages=msg)
                _check("API key valid", "PASS", "openai key works")
            except Exception as e:
                _check("API key valid", "FAIL", f"openai: {e}", fix="Check your OPENAI_API_KEY")
        else:
            _check("API key valid", "FAIL", "no key to validate", fix=f"Add your key to {config.env_file}")

    # 5. Memory directory
    if config.memory_dir.exists():
        _check("Memory directory", "PASS", str(config.memory_dir))
    else:
        _check("Memory directory", "FAIL", "missing", fix="Run: om install")

    # 6. Env file permissions
    if config.env_file.exists():
        mode = config.env_file.stat().st_mode & 0o777
        if mode == 0o600:
            _check("Env file permissions", "PASS", "600 (owner-only)")
        else:
            _check("Env file permissions", "WARN", f"{oct(mode)} (too open)", fix=f"Run: chmod 600 {config.env_file}")
    else:
        _check("Env file permissions", "WARN", "env file not found", fix="Run: om install")

    # 7. jq installed
    if shutil.which("jq"):
        _check("jq installed", "PASS", shutil.which("jq"))
    else:
        _check("jq installed", "FAIL", "not found", fix="Install with: brew install jq")

    # 8. Claude hooks
    if config.claude_settings_path.exists():
        try:
            settings = json_mod.loads(config.claude_settings_path.read_text())
            hooks = settings.get("hooks", {})
            expected = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact"]
            present = [h for h in expected if h in hooks]
            missing = [h for h in expected if h not in hooks]
            if not missing:
                _check("Claude hooks", "PASS", f"{len(present)}/4 hooks installed")
            else:
                _check("Claude hooks", "FAIL", f"missing: {', '.join(missing)}", fix="Run: om install --claude")
        except Exception as e:
            _check("Claude hooks", "FAIL", f"error reading settings: {e}", fix="Check ~/.claude/settings.json")
    else:
        _check("Claude hooks", "WARN", "settings.json not found", fix="Run: om install --claude")

    # 9. Hook paths valid (only check hooks that look like file paths, not inline shell commands)
    if config.claude_settings_path.exists():
        try:
            settings = json_mod.loads(config.claude_settings_path.read_text())
            hooks = settings.get("hooks", {})
            broken = []
            for event_name, event_hooks in hooks.items():
                for group in event_hooks:
                    for hook in group.get("hooks", []):
                        cmd = hook.get("command", "")
                        # Only validate commands that look like file paths (start with / or ~),
                        # skip inline shell commands
                        if cmd and (cmd.startswith("/") or cmd.startswith("~")) and not Path(cmd).exists():
                            broken.append(f"{event_name}: {cmd}")
            if not broken:
                _check("Hook paths valid", "PASS", "all hook commands exist")
            else:
                _check("Hook paths valid", "FAIL", f"broken: {', '.join(broken)}", fix="Run: om install --claude")
        except Exception:
            pass  # Already reported above

    # 10. Cron jobs
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and "om" in result.stdout:
            om_lines = [
                line for line in result.stdout.splitlines() if "om " in line and not line.strip().startswith("#")
            ]
            _check("Cron jobs", "PASS", f"{len(om_lines)} job(s) found")
        else:
            _check("Cron jobs", "WARN", "no observational-memory cron jobs found", fix="Run: om install")
    except Exception:
        _check("Cron jobs", "WARN", "could not read crontab")

    # 11. Platform
    if sys.platform == "win32":
        _check("Platform", "WARN", "Windows — some features may not work")
    else:
        _check("Platform", "PASS", sys.platform)

    # Output
    if as_json:
        click.echo(json_mod.dumps(results, indent=2))
    else:
        for r in results:
            tag = r["status"]
            if tag == "PASS":
                prefix = click.style("[PASS]", fg="green")
            elif tag == "WARN":
                prefix = click.style("[WARN]", fg="yellow")
            else:
                prefix = click.style("[FAIL]", fg="red")
            line = f"{prefix} {r['name']}: {r['detail']}"
            if r["fix"]:
                line += click.style(f" — {r['fix']}", fg="yellow")
            click.echo(line)

        # Summary
        passes = sum(1 for r in results if r["status"] == "PASS")
        warns = sum(1 for r in results if r["status"] == "WARN")
        fails = sum(1 for r in results if r["status"] == "FAIL")
        click.echo(f"\n{passes} passed, {warns} warnings, {fails} failures")


# --- Claude Code hook installation ---


def _install_claude_hooks(config: Config) -> None:
    """Add SessionStart and session checkpoint hooks to ~/.claude/settings.json."""
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
                    "statusMessage": "Loading observational memory...",
                }
            ]
        }
    ]

    # SessionEnd hook
    hooks["SessionEnd"] = [
        {"hooks": [{"type": "command", "command": str(session_end_hook), "timeout": 60, "async": True}]}
    ]

    # UserPromptSubmit checkpoint hook
    hooks["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": str(session_end_hook), "timeout": 5, "async": True}]}
    ]

    # PreCompact checkpoint hook
    hooks["PreCompact"] = [
        {"hooks": [{"type": "command", "command": str(session_end_hook), "timeout": 5, "async": True}]}
    ]

    config.claude_settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    click.echo("Installed Claude Code hooks (SessionStart, UserPromptSubmit, PreCompact, SessionEnd)")


def _uninstall_claude_hooks(config: Config) -> None:
    """Remove observational memory hooks from Claude Code settings."""
    import json

    if not config.claude_settings_path.exists():
        return

    settings = json.loads(config.claude_settings_path.read_text())
    hooks = settings.get("hooks", {})
    hooks.pop("SessionStart", None)
    hooks.pop("SessionEnd", None)
    hooks.pop("UserPromptSubmit", None)
    hooks.pop("PreCompact", None)
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

If this is a long-lived Codex session, Codex observations run every 15 minutes by default.
To adjust that interval, edit `~/.config/observational-memory/env` and set
`OM_CODEX_OBSERVER_INTERVAL_MINUTES` (for example: `OM_CODEX_OBSERVER_INTERVAL_MINUTES=5`).
You can run a manual checkpoint with `om observe --source codex`.

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


def _codex_observer_interval_minutes(default: int = 15) -> int:
    raw_interval = os.environ.get("OM_CODEX_OBSERVER_INTERVAL_MINUTES", str(default))
    try:
        interval = int(raw_interval)
    except ValueError:
        click.echo(
            f"Warning: invalid OM_CODEX_OBSERVER_INTERVAL_MINUTES={raw_interval!r}; using default {default}.",
            err=True,
        )
        return default

    if interval <= 0:
        click.echo(
            f"Warning: OM_CODEX_OBSERVER_INTERVAL_MINUTES must be >0; using default {default}.",
            err=True,
        )
        return default
    return min(interval, 59)


def _cron_every_minutes(minutes: int) -> str:
    if minutes <= 1:
        return "*"
    return f"*/{minutes}"


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

    codex_interval = _cron_every_minutes(_codex_observer_interval_minutes())

    if targets in ("codex", "both"):
        # Observer cron for Codex (Claude uses hooks instead)
        jobs.append(f"{codex_interval} * * * * {prefix}{om_path} observe --source codex 2>/dev/null")

    # Daily reflector for all
    jobs.append(f"0 4 * * * {prefix}{om_path} reflect 2>/dev/null")

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        existing = ""

    # Remove old OM cron lines
    lines = [line for line in existing.splitlines() if "om observe" not in line and "om reflect" not in line]

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
