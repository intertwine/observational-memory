"""CLI entry points: om observe, om reflect, om backfill, om search, om context, om install, om status, om doctor."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__
from .config import Config

_OBSERVE_SOURCES = ["claude", "codex", "hermes", "claude-memory", "all"]


@click.group()
@click.version_option(__version__, prog_name="om")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Observational Memory — shared memory for Claude Code, Codex CLI, and Hermes Agent."""
    ctx.ensure_object(dict)
    Config().load_env_file()  # Seed os.environ before constructing final config
    config = Config()
    ctx.obj["config"] = config


@cli.command()
@click.option("--transcript", type=click.Path(exists=True, path_type=Path), help="Specific transcript file to process")
@click.option(
    "--source",
    type=click.Choice(_OBSERVE_SOURCES),
    default="all",
    help="Which agent or memory source to process",
)
@click.option("--dry-run", is_flag=True, help="Print observations without writing")
@click.pass_context
def observe(ctx: click.Context, transcript: Path | None, source: str, dry_run: bool) -> None:
    """Run the observer to compress transcripts into observations."""
    from .observe import (
        observe_all_claude,
        observe_all_codex,
        observe_all_hermes,
        observe_auto_memory,
        observe_claude_transcript,
        observe_codex_transcript,
        observe_hermes_transcript,
    )

    config = ctx.obj["config"]

    if transcript:
        click.echo(f"Processing transcript: {transcript}")
        transcript_source = source
        if transcript_source == "all":
            transcript_source = _detect_transcript_source(transcript, config)

        if transcript_source == "claude":
            result = observe_claude_transcript(transcript, config, dry_run)
        elif transcript_source == "codex":
            result = observe_codex_transcript(transcript, config, dry_run)
        elif transcript_source == "hermes":
            result = observe_hermes_transcript(transcript, config, dry_run)
        elif transcript_source == "claude-memory":
            raise click.ClickException("--transcript does not support --source claude-memory.")
        else:
            raise click.ClickException(
                "Could not detect transcript source. Pass --source claude, --source codex, or --source hermes."
            )
        if result:
            click.echo(f"Observations updated ({len(result)} chars)")
            if dry_run:
                click.echo(result)
        else:
            click.echo("No new messages to process.")
        if not dry_run:
            _maybe_run_reflector_catchup(config)
        return

    results = []
    if source in ("claude", "all"):
        click.echo("Scanning Claude Code transcripts...")
        results.extend(observe_all_claude(config, dry_run))

    if source in ("codex", "all"):
        click.echo("Scanning Codex sessions...")
        results.extend(observe_all_codex(config, dry_run))

    if source in ("hermes", "all"):
        click.echo("Scanning Hermes sessions...")
        results.extend(observe_all_hermes(config=config, dry_run=dry_run))

    if source in ("claude-memory", "all"):
        click.echo("Scanning Claude Code auto-memory files...")
        changed, deleted = observe_auto_memory(config, dry_run)
        if changed or deleted:
            if changed:
                click.echo(f"  {len(changed)} file(s) changed:")
                for path in changed:
                    click.echo(f"    {path}")
            if deleted:
                click.echo(f"  {len(deleted)} file(s) removed:")
                for path in deleted:
                    click.echo(f"    {path}")
        else:
            click.echo("  No changes detected.")

    if results:
        click.echo(f"Processed {len(results)} transcript(s)")
        if dry_run:
            for r in results:
                click.echo("---")
                click.echo(r)
    else:
        if source not in ("claude-memory",):
            click.echo("No new messages to process.")

    if not dry_run:
        _maybe_run_reflector_catchup(config)


def _detect_transcript_source(transcript: Path, config: Config) -> str | None:
    """Best-effort transcript source detection for explicit single-file observe."""
    try:
        transcript.relative_to(config.claude_projects_dir)
        return "claude"
    except ValueError:
        pass

    try:
        transcript.relative_to(config.codex_home / "sessions")
        return "codex"
    except ValueError:
        pass

    try:
        transcript.relative_to(config.hermes_sessions_dir)
        return "hermes"
    except ValueError:
        pass

    return None


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


def _maybe_run_reflector_catchup(config: Config) -> None:
    """Run the reflector when daily reflections have fallen behind observations."""
    from .reflect import reflector_catchup_needed, run_reflector

    if not reflector_catchup_needed(config):
        return

    click.echo("Running reflector catch-up...")
    try:
        result = run_reflector(config)
    except Exception as e:
        click.echo(f"Reflector catch-up failed: {e}")
        return

    if result:
        click.echo(f"Reflections updated ({len(result)} chars)")
    else:
        click.echo("No observations to reflect on.")


@cli.command()
@click.option(
    "--source",
    type=click.Choice(["claude", "codex", "claude-memory", "all"]),
    default="all",
    help="Which transcripts to process",
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
@click.option("--raw-qmd", is_flag=True, help="Pass through native qmd output (QMD backends only)")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, reindex: bool, as_json: bool, raw_qmd: bool) -> None:
    """Search observations and reflections for relevant memories."""
    from .search import get_backend
    from .search import reindex as do_reindex

    config = ctx.obj["config"]

    if raw_qmd and as_json:
        raise click.ClickException("--raw-qmd cannot be combined with --json.")

    if reindex:
        n = do_reindex(config)
        if not as_json and not raw_qmd:
            click.echo(f"Indexed {n} document(s)")

    backend = get_backend(config.search_backend, config)

    if not backend.is_ready():
        # Auto-index on first search
        n = do_reindex(config)
        if not as_json and not raw_qmd:
            click.echo(f"Built index ({n} document(s))")

    if raw_qmd:
        if not hasattr(backend, "raw_search_output"):
            raise click.ClickException("--raw-qmd is only available with qmd and qmd-hybrid backends.")
        stdout, stderr, returncode = backend.raw_search_output(query, limit=limit)
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or "qmd search failed"
            raise click.ClickException(detail)
        if stdout:
            click.echo(stdout, nl=not stdout.endswith("\n"))
        return

    results = backend.search(query, limit=limit)

    if as_json:
        import json as json_mod

        output = [_search_result_payload(r) for r in results]
        click.echo(json_mod.dumps(output, indent=2))
    elif results:
        for r in results:
            click.echo(f"\n--- [{r.rank}] {r.document.heading} (score: {r.score:.2f}) ---")
            payload = _search_result_payload(r)
            source_location = _format_location(payload["source_path"], payload["source_line"])
            qmd_location = _format_location(payload["qmd_file"], payload["qmd_line"])
            if source_location:
                click.echo(f"  Source: {source_location}")
            if qmd_location:
                click.echo(f"  QMD hit: {qmd_location}")
            # Show first 5 lines of content
            lines = r.document.content.strip().splitlines()
            for line in lines[:5]:
                click.echo(f"  {line}")
            if len(lines) > 5:
                click.echo(f"  ... ({len(lines) - 5} more lines)")
    else:
        click.echo("No results found.")


def _search_result_payload(result) -> dict[str, object]:
    """Normalize a search result for JSON and terminal rendering."""
    metadata = dict(result.document.metadata)
    metadata.pop("source_start_line", None)
    qmd_line = metadata.get("qmd_line", metadata.get("line"))
    return {
        "rank": result.rank,
        "score": result.score,
        "doc_id": result.document.doc_id,
        "source": result.document.source.value,
        "heading": result.document.heading,
        "content": result.document.content[:500],
        "source_path": metadata.get("file_path"),
        "source_line": metadata.get("source_line"),
        "qmd_file": metadata.get("qmd_file"),
        "qmd_docid": metadata.get("qmd_docid"),
        "qmd_line": qmd_line,
        "metadata": metadata,
    }


def _format_location(path: str | None, line: int | None) -> str | None:
    """Render an optional path[:line] string for search output."""
    if not path:
        return None
    if line is None:
        return str(path)
    return f"{path}:{line}"


@cli.command(hidden=True)
@click.pass_context
def context(ctx: click.Context) -> None:
    """Generate session-start JSON with search-backed memory retrieval.

    Called by the SessionStart hook. Outputs JSON with additionalContext
    containing full reflections + search results (or full observations as fallback).
    """
    import json as json_mod

    from .search import get_backend
    from .startup_memory import ensure_startup_memory

    config = ctx.obj["config"]
    ensure_startup_memory(config)
    parts = []

    if config.profile_path.exists() and config.profile_path.stat().st_size > 0:
        parts.append(config.profile_path.read_text())

    if config.active_path.exists() and config.active_path.stat().st_size > 0:
        parts.append(config.active_path.read_text())

    # Backward-compatible fallback for older installs if derived files are unavailable.
    if not parts:
        if config.reflections_path.exists() and config.reflections_path.stat().st_size > 0:
            parts.append("## Long-Term Memory (Reflections)\n\n" + config.reflections_path.read_text())

        observations_added = False
        backend = get_backend(config.search_backend, config)
        if backend.is_ready():
            results = backend.search("recent context current tasks projects", limit=10)
            if results:
                obs_parts = []
                for r in results:
                    if r.document.source.value == "observations":
                        obs_parts.append(r.document.content)
                if obs_parts:
                    parts.append("## Recent Observations\n\n" + "\n\n".join(obs_parts))
                    observations_added = True

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


@cli.command(hidden=True, name="codex-checkpoint")
@click.pass_context
def codex_checkpoint(ctx: click.Context) -> None:
    """Queue a Codex transcript-specific checkpoint from the Stop hook payload."""
    import json as json_mod
    import subprocess

    config = ctx.obj["config"]

    try:
        payload = json_mod.load(sys.stdin)
    except json_mod.JSONDecodeError:
        return

    if not isinstance(payload, dict):
        return

    transcript_raw = payload.get("transcript_path")
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return

    transcript = Path(transcript_raw).expanduser()
    if not transcript.is_file():
        return

    lock_path = _codex_checkpoint_lock_path(config, transcript)
    if not _acquire_codex_checkpoint_lock(config, lock_path):
        return

    try:
        current_count = _count_codex_transcript_messages(transcript)
        if current_count <= 0:
            _release_codex_checkpoint_lock(lock_path)
            return

        state = _load_codex_checkpoint_state(config)
        previous = state.get(str(transcript), {})
        previous_count = previous.get("message_count")
        previous_status = previous.get("status")
        if (
            isinstance(previous_count, int)
            and previous_count >= current_count
            and previous_status in {"in_progress", "success"}
        ):
            _release_codex_checkpoint_lock(lock_path)
            return

        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=current_count,
            status="in_progress",
        )

        worker_command = _build_codex_checkpoint_worker_command(transcript)
        subprocess.Popen(
            worker_command,
            cwd=payload.get("cwd") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="failed",
        )
        _release_codex_checkpoint_lock(lock_path)
        raise


@cli.command(hidden=True, name="codex-checkpoint-worker")
@click.option("--transcript", type=click.Path(path_type=Path), required=True)
@click.pass_context
def codex_checkpoint_worker(ctx: click.Context, transcript: Path) -> None:
    """Observe one Codex transcript and release its checkpoint lock."""
    from .observe import observe_codex_transcript

    config = ctx.obj["config"]
    transcript = transcript.expanduser()
    lock_path = _codex_checkpoint_lock_path(config, transcript)

    try:
        observe_codex_transcript(transcript, config, dry_run=False)
        _maybe_run_reflector_catchup(config)
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="success",
        )
    except Exception:
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="failed",
        )
        raise
    finally:
        _release_codex_checkpoint_lock(lock_path)


_SUPPORTED_PROVIDERS = ("anthropic", "openai", "anthropic-vertex", "anthropic-bedrock")
_SCHEDULER_MODES = ("auto", "launchd", "cron", "none")
_SCHEDULER_COMMAND_TIMEOUT_SECONDS = 5


def _validate_api_key_format(key: str, provider: str) -> bool:
    """Basic format validation for API keys."""
    if provider == "anthropic":
        return key.startswith("sk-ant-") and len(key) > 20
    elif provider == "openai":
        return key.startswith("sk-") and len(key) > 20
    return False


def _upsert_env_vars(env_file: Path, updates: dict[str, str | None]) -> None:
    """Upsert env vars while preserving unrelated lines/comments."""
    cleaned = {k: v for k, v in updates.items() if v is not None}
    if not cleaned:
        return

    if env_file.exists():
        lines = env_file.read_text().splitlines()
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        lines = []

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = None
        if "=" in stripped:
            candidate = stripped
            if candidate.startswith("#"):
                candidate = candidate[1:].strip()
            key = candidate.split("=", 1)[0].strip()

        if key in cleaned:
            if key not in seen:
                new_lines.append(f"{key}={cleaned[key]}")
                seen.add(key)
            continue

        new_lines.append(line)

    for key, value in cleaned.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    env_file.write_text("\n".join(new_lines).rstrip() + "\n")
    env_file.chmod(0o600)


def _provider_api_key_env(provider: str) -> str | None:
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider == "openai":
        return "OPENAI_API_KEY"
    return None


def _import_provider_sdk(provider: str) -> None:
    if provider in {"anthropic", "anthropic-vertex", "anthropic-bedrock"}:
        try:
            import anthropic  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "Missing 'anthropic' SDK. Install enterprise extras: uv tool install 'observational-memory[enterprise]'"
            ) from e

    if provider == "openai":
        try:
            import openai  # noqa: F401
        except Exception as e:
            raise RuntimeError("Missing 'openai' SDK. Install with: uv tool install observational-memory") from e

    if provider == "anthropic-vertex":
        try:
            import google.auth  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "Missing 'google-auth' dependency for Vertex. Install enterprise extras: "
                "uv tool install 'observational-memory[enterprise]'"
            ) from e

    if provider == "anthropic-bedrock":
        try:
            import boto3  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "Missing 'boto3' dependency for Bedrock. Install enterprise extras: "
                "uv tool install 'observational-memory[enterprise]'"
            ) from e


def _validate_llm_access(config: Config) -> str:
    from .llm import compress

    provider = config.validate_provider_config()
    compress(
        "You are a health check. Reply with OK.",
        "Reply with exactly: OK",
        config,
        max_tokens=8,
        operation="observer",
    )
    return provider


def _configure_llm(
    config: Config,
    provider: str | None,
    llm_model: str | None,
    vertex_project_id: str | None,
    vertex_region: str | None,
    bedrock_region: str | None,
    non_interactive: bool,
) -> None:
    """Configure provider/model/auth settings for install."""
    selected = provider.lower() if provider else None
    if selected and selected not in _SUPPORTED_PROVIDERS:
        raise click.ClickException(f"Unsupported provider '{provider}'.")

    if selected is None:
        if non_interactive:
            try:
                selected = config.resolve_provider()
            except RuntimeError as e:
                raise click.ClickException(
                    "No provider configured. Use --provider and provider-specific flags in non-interactive mode."
                ) from e
        else:
            default_provider = "anthropic"
            try:
                default_provider = config.resolve_provider()
            except RuntimeError:
                pass
            selected = click.prompt(
                "Which provider?",
                type=click.Choice(list(_SUPPORTED_PROVIDERS), case_sensitive=False),
                default=default_provider,
            ).lower()

    updates: dict[str, str | None] = {"OM_LLM_PROVIDER": selected}

    model = llm_model
    if not model and not non_interactive:
        current = config.llm_model or config.resolve_model(provider=selected)
        model = click.prompt("Model", default=current).strip()
    if model:
        updates["OM_LLM_MODEL"] = model

    if selected in {"anthropic", "openai"}:
        env_var = _provider_api_key_env(selected)
        assert env_var is not None
        existing = os.environ.get(env_var)
        if not existing:
            if non_interactive:
                raise click.ClickException(
                    f"Provider '{selected}' requires {env_var}. Set it in env "
                    f"or {config.env_file} before --non-interactive install."
                )
            key = click.prompt(f"Paste your {selected} API key", hide_input=True).strip()
            if key:
                if not _validate_api_key_format(key, selected):
                    click.echo(f"Warning: key doesn't match expected {selected} format, saving anyway.")
                updates[env_var] = key
                os.environ[env_var] = key

        if not non_interactive and click.confirm("Validate LLM access now?", default=False):
            trial = Config(
                llm_provider=selected,
                llm_model=updates.get("OM_LLM_MODEL") or config.llm_model,
                anthropic_model=config.anthropic_model,
                openai_model=config.openai_model,
                vertex_project_id=config.vertex_project_id,
                vertex_region=config.vertex_region,
                bedrock_region=config.bedrock_region,
                env_file=config.env_file,
            )
            try:
                _validate_llm_access(trial)
                click.echo("LLM access validated.")
            except Exception as e:
                click.echo(f"Warning: LLM access validation failed: {e}")

    elif selected == "anthropic-vertex":
        project = vertex_project_id or config.vertex_project_id
        region = vertex_region or config.vertex_region
        if not project and not non_interactive:
            project = click.prompt("Vertex project ID").strip()
        if not region and not non_interactive:
            region = click.prompt("Vertex region", default="us-east5").strip()
        if not project or not region:
            raise click.ClickException(
                "Provider 'anthropic-vertex' requires --vertex-project-id and --vertex-region (or existing env values)."
            )
        updates["OM_VERTEX_PROJECT_ID"] = project
        updates["OM_VERTEX_REGION"] = region
        os.environ["OM_VERTEX_PROJECT_ID"] = project
        os.environ["OM_VERTEX_REGION"] = region

    elif selected == "anthropic-bedrock":
        region = bedrock_region or config.bedrock_region or os.environ.get("AWS_REGION")
        if not region and not non_interactive:
            region = click.prompt("Bedrock region", default="us-east-1").strip()
        if not region:
            raise click.ClickException(
                "Provider 'anthropic-bedrock' requires --bedrock-region (or OM_BEDROCK_REGION/AWS_REGION)."
            )
        updates["OM_BEDROCK_REGION"] = region
        os.environ["OM_BEDROCK_REGION"] = region

    updates["OM_LLM_PROVIDER"] = selected
    if "OM_LLM_MODEL" in updates:
        os.environ["OM_LLM_MODEL"] = updates["OM_LLM_MODEL"] or ""
    os.environ["OM_LLM_PROVIDER"] = selected

    _upsert_env_vars(config.env_file, updates)
    click.echo(f"Configured LLM provider '{selected}' in {config.env_file}")


@cli.command()
@click.option("--claude", "targets", flag_value="claude", help="Install Claude Code hooks")
@click.option("--codex", "targets", flag_value="codex", help="Install Codex hooks plus AGENTS fallback")
@click.option("--both", "targets", flag_value="both", default=True, help="Install both (default)")
@click.option(
    "--scheduler",
    type=click.Choice(_SCHEDULER_MODES, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Background scheduler backend",
)
@click.option("--cron/--no-cron", "cron_compat", default=None, help="Legacy alias for --scheduler cron/none")
@click.option(
    "--provider",
    type=click.Choice(list(_SUPPORTED_PROVIDERS), case_sensitive=False),
    help="LLM provider profile (anthropic, openai, anthropic-vertex, anthropic-bedrock)",
)
@click.option("--llm-model", help="Shared model name for observer + reflector")
@click.option("--vertex-project-id", help="GCP project ID for anthropic-vertex")
@click.option("--vertex-region", help="GCP region for anthropic-vertex (for example: us-east5)")
@click.option("--bedrock-region", help="AWS region for anthropic-bedrock (for example: us-east-1)")
@click.option("--non-interactive", is_flag=True, help="Do not prompt; require all needed config via flags/env")
@click.pass_context
def install(
    ctx: click.Context,
    targets: str,
    scheduler: str,
    cron_compat: bool | None,
    provider: str | None,
    llm_model: str | None,
    vertex_project_id: str | None,
    vertex_region: str | None,
    bedrock_region: str | None,
    non_interactive: bool,
) -> None:
    """Set up observational memory for Claude Code and/or Codex CLI."""
    config = ctx.obj["config"]
    config.ensure_memory_dir()
    scheduler_mode = _resolve_scheduler_mode(scheduler, cron_compat)

    # Create env file for provider/auth config
    if config.ensure_env_file():
        click.echo(f"Created {config.env_file}")
        click.echo(f"  Add your LLM provider settings: {config.env_file}")
    else:
        click.echo(f"Env file: {config.env_file} (already exists)")

    _configure_llm(
        config,
        provider=provider,
        llm_model=llm_model,
        vertex_project_id=vertex_project_id,
        vertex_region=vertex_region,
        bedrock_region=bedrock_region,
        non_interactive=non_interactive,
    )

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

    from .startup_memory import refresh_startup_memory

    refresh_startup_memory(config)
    click.echo(f"Created {config.profile_path}")
    click.echo(f"Created {config.active_path}")

    if targets in ("claude", "both"):
        _install_claude_hooks(config)

    if targets in ("codex", "both"):
        _install_codex(config)

    if scheduler_mode == "launchd":
        try:
            _install_launchd(config, targets)
        except click.ClickException as exc:
            click.echo(f"Warning: launchd scheduler setup failed: {exc}", err=True)
        else:
            _uninstall_cron(targets)
    elif scheduler_mode == "cron":
        _install_cron(config, targets)
        if sys.platform == "darwin":
            _uninstall_launchd(config, targets)
    elif scheduler_mode == "none":
        if sys.platform == "darwin":
            _uninstall_launchd(config, targets)
        _uninstall_cron(targets)

    click.echo("\nInstallation complete! Run 'om status' to verify.")


@cli.command()
@click.option("--claude", "targets", flag_value="claude")
@click.option("--codex", "targets", flag_value="codex")
@click.option("--both", "targets", flag_value="both", default=True)
@click.option("--purge", is_flag=True, help="Also remove memory files")
@click.pass_context
def uninstall(ctx: click.Context, targets: str, purge: bool) -> None:
    """Remove observational memory hooks and background scheduler jobs."""
    config = ctx.obj["config"]

    if targets in ("claude", "both"):
        _uninstall_claude_hooks(config)

    if targets in ("codex", "both"):
        _uninstall_codex(config)

    _uninstall_launchd(config, targets)
    _uninstall_cron(targets)

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

    # Compact startup files
    if config.profile_path.exists():
        profile = config.profile_path.read_text()
        click.echo(f"\nStartup profile: {config.profile_path}")
        click.echo(f"  Lines: {len(profile.splitlines())}, Size: {len(profile)} bytes")
    else:
        click.echo("\nStartup profile: not created yet")

    if config.active_path.exists():
        active = config.active_path.read_text()
        click.echo(f"\nActive context: {config.active_path}")
        click.echo(f"  Lines: {len(active.splitlines())}, Size: {len(active)} bytes")
    else:
        click.echo("\nActive context: not created yet")

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

    # Search backend
    click.echo("\nSearch:")
    click.echo(f"  Backend: {config.search_backend}")
    if config.search_backend == "bm25":
        click.echo(f"  BM25 index: {config.search_index_dir / 'bm25.pkl'}")
    elif config.search_backend in {"qmd", "qmd-hybrid"}:
        from .search.qmd import QMDBackend, inspect_qmd_index, inspect_qmd_install

        install = inspect_qmd_install()
        if not install.available:
            click.echo("  QMD binary: not installed")
        else:
            click.echo(f"  QMD binary: {install.binary_path}")
            click.echo(f"  QMD index: {config.qmd_index_name}")
            if install.supports_no_rerank:
                feature_status = "detected (--no-rerank available)"
            elif install.supports_bench:
                feature_status = "partial (bench subcommand detected)"
            else:
                feature_status = "not detected"
            click.echo(f"  QMD 2.1 features: {feature_status}")
            if config.search_backend == "qmd-hybrid":
                rerank_status = "disabled via OM_QMD_NO_RERANK=1" if config.qmd_no_rerank else "enabled"
                click.echo(f"  Hybrid rerank: {rerank_status}")

            status = inspect_qmd_index(
                config.qmd_index_name,
                QMDBackend.COLLECTION_NAME,
                env_overrides=config.qmd_model_env(),
            )
            if status.error:
                click.echo(f"  QMD status: error ({status.error})")
            elif not status.collection_exists:
                click.echo(f"  Collection: {QMDBackend.COLLECTION_NAME} not indexed yet")
            else:
                click.echo(f"  Collection: {QMDBackend.COLLECTION_NAME}")
                if status.index_path:
                    click.echo(f"  Index path: {status.index_path}")
                if status.total_files is not None:
                    click.echo(f"  Indexed files: {status.total_files}")
                if status.vectors_embedded is not None:
                    click.echo(f"  Embedded vectors: {status.vectors_embedded}")
                if status.pending_vectors is not None:
                    click.echo(f"  Pending vectors: {status.pending_vectors}")
                if status.updated:
                    click.echo(f"  Updated: {status.updated}")

        model_env = config.qmd_model_env()
        if model_env:
            click.echo("  Model overrides: " + ", ".join(f"{key}={value}" for key, value in sorted(model_env.items())))

    # LLM provider/model status
    click.echo("\nLLM:")
    try:
        provider = config.resolve_provider()
        click.echo(f"  Provider: {provider}")
        click.echo(f"  Observer model: {config.resolve_model(operation='observer', provider=provider)}")
        click.echo(f"  Reflector model: {config.resolve_model(operation='reflector', provider=provider)}")
    except RuntimeError as e:
        click.echo(f"  Provider: unresolved ({e})")

    click.echo(f"  Anthropic API key: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    click.echo(f"  OpenAI API key: {'set' if os.environ.get('OPENAI_API_KEY') else 'not set'}")
    click.echo(f"  Vertex project: {config.vertex_project_id or 'not set'}")
    click.echo(f"  Vertex region: {config.vertex_region or 'not set'}")
    click.echo(f"  Bedrock region: {config.bedrock_region or os.environ.get('AWS_REGION') or 'not set'}")

    if config.llm_provider == "anthropic-vertex":
        click.echo("  Auth mode: Google ADC (application default credentials)")
    elif config.llm_provider == "anthropic-bedrock":
        click.echo("  Auth mode: AWS credential chain (profile/role/env)")

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

    click.echo("\nCodex hooks integration:")
    feature_enabled, feature_error = _codex_hooks_feature_enabled(config)
    if feature_error:
        click.echo(f"  Hook feature: error ({feature_error})")
    elif feature_enabled is None:
        click.echo(f"  Hook feature: config not found at {config.codex_config_path}")
    else:
        click.echo(f"  Hook feature: {'enabled' if feature_enabled else 'disabled'}")

    session_start_hook, hooks_error = _find_codex_session_start_hook(config)
    stop_hook, stop_error = _find_codex_stop_hook(config)
    if hooks_error:
        click.echo(f"  hooks.json: error ({hooks_error})")
    else:
        click.echo(f"  hooks.json: {'found' if config.codex_hooks_path.exists() else 'not found'}")
        click.echo(f"  SessionStart: {'installed' if session_start_hook else 'not installed'}")
        if stop_error:
            click.echo(f"  Stop: error ({stop_error})")
        else:
            click.echo(f"  Stop: {'installed' if stop_hook else 'not installed'}")

    agents_status = _codex_agents_fallback_status(config)
    if agents_status == "fallback":
        click.echo("  AGENTS fallback: installed")
    elif agents_status == "legacy":
        click.echo("  AGENTS fallback: legacy OM block present")
    elif config.codex_agents_md.exists():
        click.echo("  AGENTS fallback: not installed")
    else:
        click.echo(f"  AGENTS fallback: AGENTS.md not found at {config.codex_agents_md}")

    click.echo("\nBackground scheduler:")
    click.echo(f"  Default backend: {_resolve_scheduler_mode('auto', None)}")

    launchd_jobs = _launchd_job_statuses(config) if sys.platform == "darwin" else []
    if sys.platform == "darwin":
        installed_jobs = [job for job in launchd_jobs if job["installed"]]
        loaded_jobs = [job for job in launchd_jobs if job["loaded"]]
        missing_jobs = [str(job["key"]) for job in launchd_jobs if not job["installed"]]
        load_errors = [f"{job['key']}: {job['error']}" for job in launchd_jobs if job["error"]]

        click.echo(f"  LaunchAgents: {len(installed_jobs)}/{len(launchd_jobs)} installed")
        if installed_jobs:
            click.echo(f"  Loaded: {len(loaded_jobs)}/{len(installed_jobs)} loaded")
        else:
            click.echo("  Loaded: none")

        if missing_jobs:
            click.echo(f"  Missing: {', '.join(missing_jobs)}")
        if load_errors:
            click.echo(f"  launchctl: {', '.join(load_errors)}")

    cron_jobs, cron_error = _om_cron_jobs()
    if cron_error:
        click.echo(f"  Cron jobs: error ({cron_error})")
    elif cron_jobs:
        click.echo(f"  Cron jobs: {len(cron_jobs)} found ({', '.join(sorted(cron_jobs))})")
        if sys.platform == "darwin" and any(job["installed"] for job in launchd_jobs):
            click.echo("  Duplicate backstops: launchd and cron are both present")
    else:
        click.echo("  Cron jobs: none")

    # Auto-memory (Claude Code per-project memory)
    from .transcripts.auto_memory import find_memory_directories

    memory_dirs = find_memory_directories(config.claude_projects_dir)
    if memory_dirs:
        total_files = sum(len(list(d.glob("*.md"))) for d in memory_dirs)
        click.echo(f"\nAuto-memory: {len(memory_dirs)} project(s), {total_files} file(s)")
        amem_cursor = cursor.get("claude-memory", {})
        tracked = len(amem_cursor.get("files", {}))
        last_scan = amem_cursor.get("last_scan", "never")
        click.echo(f"  Tracked: {tracked} file(s), last scan: {last_scan}")
    else:
        click.echo("\nAuto-memory: no project memory directories found")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--validate-key", is_flag=True, help="Test configured LLM access with a live API call")
@click.pass_context
def doctor(ctx: click.Context, as_json: bool, validate_key: bool) -> None:
    """Run diagnostic checks on your observational memory installation."""
    import json as json_mod

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

    # 3. Provider config
    resolved_provider: str | None = None
    try:
        resolved_provider = config.validate_provider_config()
        _check("LLM provider config", "PASS", resolved_provider)
    except Exception as e:
        _check("LLM provider config", "FAIL", str(e), fix="Run: om install --provider <provider>")

    # 4. Provider SDK dependencies
    if resolved_provider:
        try:
            _import_provider_sdk(resolved_provider)
            _check("LLM SDK dependencies", "PASS", f"{resolved_provider} dependencies available")
        except Exception as e:
            _check(
                "LLM SDK dependencies",
                "FAIL",
                str(e),
                fix="Install enterprise extras if needed: uv tool install 'observational-memory[enterprise]'",
            )
    else:
        _check("LLM SDK dependencies", "WARN", "skipped (provider not configured)")

    # 5. Validate configured access (opt-in)
    if validate_key:
        if not resolved_provider:
            _check(
                "Configured LLM access",
                "FAIL",
                "no provider configured",
                fix="Run: om install --provider <provider>",
            )
        else:
            try:
                provider = _validate_llm_access(config)
                _check("Configured LLM access", "PASS", f"{provider} call succeeded")
            except Exception as e:
                _check("Configured LLM access", "FAIL", str(e), fix="Check provider auth/config and retry")

    # 6. Memory directory
    if config.memory_dir.exists():
        _check("Memory directory", "PASS", str(config.memory_dir))
    else:
        _check("Memory directory", "FAIL", "missing", fix="Run: om install")

    # 7. Env file permissions
    if config.env_file.exists():
        mode = config.env_file.stat().st_mode & 0o777
        if mode == 0o600:
            _check("Env file permissions", "PASS", "600 (owner-only)")
        else:
            _check("Env file permissions", "WARN", f"{oct(mode)} (too open)", fix=f"Run: chmod 600 {config.env_file}")
    else:
        _check("Env file permissions", "WARN", "env file not found", fix="Run: om install")

    # 8. jq installed
    if shutil.which("jq"):
        _check("jq installed", "PASS", shutil.which("jq"))
    else:
        _check("jq installed", "FAIL", "not found", fix="Install with: brew install jq")

    # 9. QMD search backend health
    if config.search_backend in {"qmd", "qmd-hybrid"}:
        from .search.qmd import QMDBackend, inspect_qmd_index, inspect_qmd_install

        install = inspect_qmd_install()
        if install.available:
            _check("QMD binary", "PASS", install.binary_path or "qmd")
        else:
            _check(
                "QMD binary",
                "FAIL",
                install.error or "qmd not found",
                fix="Install with: npm install -g @tobilu/qmd",
            )

        if install.available:
            if install.supports_no_rerank:
                _check("QMD 2.1 features", "PASS", "--no-rerank support detected")
            elif install.supports_bench:
                _check("QMD 2.1 features", "WARN", "bench subcommand detected but --no-rerank unavailable")
            else:
                _check("QMD 2.1 features", "WARN", "--no-rerank support not detected", fix="Upgrade QMD to >= 2.1.0")

            status = inspect_qmd_index(
                config.qmd_index_name,
                QMDBackend.COLLECTION_NAME,
                env_overrides=config.qmd_model_env(),
            )
            if status.error:
                _check("QMD collection", "WARN", status.error, fix='Run: om search --reindex "test query"')
            elif status.collection_exists:
                detail = f"{QMDBackend.COLLECTION_NAME} in index {config.qmd_index_name}"
                if status.total_files is not None:
                    detail += f" ({status.total_files} files)"
                _check("QMD collection", "PASS", detail)
            else:
                _check(
                    "QMD collection",
                    "WARN",
                    f"{QMDBackend.COLLECTION_NAME} not indexed in {config.qmd_index_name}",
                    fix='Run: om search --reindex "test query"',
                )

            if config.search_backend == "qmd-hybrid":
                if config.qmd_no_rerank:
                    if install.supports_no_rerank:
                        _check("QMD rerank mode", "PASS", "disabled via OM_QMD_NO_RERANK=1")
                    else:
                        _check(
                            "QMD rerank mode",
                            "WARN",
                            "OM_QMD_NO_RERANK=1 but installed qmd does not advertise --no-rerank",
                            fix="Upgrade QMD to >= 2.1.0",
                        )
                else:
                    _check("QMD rerank mode", "PASS", "enabled")

                if status.error:
                    _check(
                        "QMD embeddings",
                        "WARN",
                        status.error,
                        fix=f"Run: qmd --index {config.qmd_index_name} embed",
                    )
                elif not status.collection_exists:
                    _check(
                        "QMD embeddings",
                        "WARN",
                        "skipped (collection not indexed yet)",
                        fix='Run: om search --reindex "test query"',
                    )
                elif status.pending_vectors is None and status.vectors_embedded is None:
                    _check("QMD embeddings", "WARN", "status output did not report embedding counts")
                elif (status.vectors_embedded or 0) <= 0:
                    _check(
                        "QMD embeddings",
                        "WARN",
                        "0 embedded vectors",
                        fix=f"Run: qmd --index {config.qmd_index_name} embed",
                    )
                elif (status.pending_vectors or 0) > 0:
                    _check(
                        "QMD embeddings",
                        "WARN",
                        f"{status.vectors_embedded} embedded, {status.pending_vectors} pending",
                        fix=f"Run: qmd --index {config.qmd_index_name} embed",
                    )
                else:
                    _check("QMD embeddings", "PASS", f"{status.vectors_embedded} embedded, 0 pending")
        else:
            if config.search_backend == "qmd-hybrid":
                _check("QMD 2.1 features", "WARN", "skipped (qmd not installed)")
                _check("QMD collection", "WARN", "skipped (qmd not installed)")
                _check("QMD rerank mode", "WARN", "skipped (qmd not installed)")
                _check("QMD embeddings", "WARN", "skipped (qmd not installed)")
            else:
                _check("QMD 2.1 features", "WARN", "skipped (qmd not installed)")
                _check("QMD collection", "WARN", "skipped (qmd not installed)")

    # 10. Claude hooks
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

    # 11. Codex startup integration
    agents_status = _codex_agents_fallback_status(config)
    has_agents_fallback = agents_status in {"fallback", "legacy"}
    feature_enabled, feature_error = _codex_hooks_feature_enabled(config)
    if feature_error:
        _check("Codex hooks feature", "FAIL", feature_error, fix="Check ~/.codex/config.toml")
    elif feature_enabled:
        _check("Codex hooks feature", "PASS", "enabled")
    elif has_agents_fallback:
        _check("Codex hooks feature", "WARN", "disabled or not configured", fix="Run: om install --codex")
    else:
        _check("Codex hooks feature", "FAIL", "disabled or not configured", fix="Run: om install --codex")

    session_start_hook, hooks_error = _find_codex_session_start_hook(config)
    if hooks_error:
        _check("Codex SessionStart hook", "FAIL", hooks_error, fix="Check ~/.codex/hooks.json")
    elif session_start_hook:
        _check("Codex SessionStart hook", "PASS", "installed")
    elif has_agents_fallback:
        _check(
            "Codex SessionStart hook",
            "WARN",
            "not installed; AGENTS fallback still available",
            fix="Run: om install --codex",
        )
    else:
        _check("Codex SessionStart hook", "FAIL", "not installed", fix="Run: om install --codex")

    stop_hook, stop_error = _find_codex_stop_hook(config)
    if stop_error:
        _check("Codex Stop hook", "FAIL", stop_error, fix="Check ~/.codex/hooks.json")
    elif stop_hook:
        _check("Codex Stop hook", "PASS", "installed")
    else:
        _check(
            "Codex Stop hook",
            "WARN",
            "not installed; background backstop still available",
            fix="Run: om install --codex",
        )

    if agents_status == "fallback":
        _check("Codex AGENTS fallback", "PASS", "installed")
    elif agents_status == "legacy":
        _check("Codex AGENTS fallback", "WARN", "legacy OM block present", fix="Run: om install --codex")
    else:
        _check("Codex AGENTS fallback", "WARN", "not installed", fix="Run: om install --codex")

    hook_commands = []
    if session_start_hook:
        hook_commands.append(("SessionStart", session_start_hook.get("command", "")))
    if stop_hook:
        hook_commands.append(("Stop", stop_hook.get("command", "")))

    if hook_commands:
        invalid = [
            f"{event}: {command or 'missing command'}"
            for event, command in hook_commands
            if not _hook_command_exists(command)
        ]
        if invalid:
            _check("Codex hook commands valid", "FAIL", ", ".join(invalid), fix="Run: om install --codex")
        else:
            _check(
                "Codex hook commands valid",
                "PASS",
                ", ".join(f"{event}: {command}" for event, command in hook_commands),
            )
    else:
        _check("Codex hook commands valid", "WARN", "skipped (Codex hooks not installed)")

    # 12. Hook paths valid (only check Claude hook commands that look like file paths, not inline shell commands)
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

    # 13. Background scheduler
    _check("Scheduler default", "PASS", _resolve_scheduler_mode("auto", None))

    launchd_jobs = _launchd_job_statuses(config) if sys.platform == "darwin" else []
    installed_launchd_jobs = [job for job in launchd_jobs if job["installed"]]
    missing_launchd_jobs = [str(job["key"]) for job in launchd_jobs if not job["installed"]]
    unloaded_launchd_jobs = [str(job["key"]) for job in installed_launchd_jobs if not job["loaded"]]
    launchd_errors = [f"{job['key']}: {job['error']}" for job in launchd_jobs if job["error"]]

    if sys.platform == "darwin":
        if installed_launchd_jobs:
            if missing_launchd_jobs:
                _check(
                    "LaunchAgents",
                    "WARN",
                    (
                        f"{len(installed_launchd_jobs)}/{len(launchd_jobs)} installed; "
                        f"missing: {', '.join(missing_launchd_jobs)}"
                    ),
                    fix="Run: om install",
                )
            else:
                _check("LaunchAgents", "PASS", f"{len(installed_launchd_jobs)}/{len(launchd_jobs)} installed")

            if launchd_errors:
                _check("LaunchAgents loaded", "WARN", ", ".join(launchd_errors), fix="Run: om install")
            elif unloaded_launchd_jobs:
                _check(
                    "LaunchAgents loaded",
                    "WARN",
                    f"not loaded: {', '.join(unloaded_launchd_jobs)}",
                    fix="Run: om install",
                )
            else:
                _check("LaunchAgents loaded", "PASS", "all installed LaunchAgents are loaded")
        else:
            _check("LaunchAgents", "WARN", "no OM LaunchAgents found", fix="Run: om install")

    cron_jobs, cron_error = _om_cron_jobs()
    if cron_error:
        _check("Cron jobs", "WARN", f"could not read crontab: {cron_error}")
    elif cron_jobs:
        if sys.platform == "darwin" and installed_launchd_jobs:
            _check(
                "Legacy cron jobs",
                "WARN",
                f"{len(cron_jobs)} OM job(s) still present alongside launchd",
                fix="Run: om install",
            )
        else:
            _check("Cron jobs", "PASS", f"{len(cron_jobs)} job(s) found")
    elif sys.platform == "darwin" and installed_launchd_jobs:
        _check("Legacy cron jobs", "PASS", "none found")
    else:
        _check("Cron jobs", "WARN", "no observational-memory background jobs found", fix="Run: om install")

    # 14. Platform
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
_CODEX_OM_FALLBACK_VERSION_MARKER = "<!-- observational-memory:codex-hooks-fallback-v1 -->"
_CODEX_HOOKS_FEATURE_FLAGS = ("hooks", "codex_hooks")
_CODEX_SESSION_START_MATCHER = "startup|resume"
_CODEX_SESSION_START_STATUS = "Loading observational memory..."
_CODEX_STOP_STATUS = "Checkpointing observational memory..."

_CODEX_OM_BLOCK = f"""{_CODEX_OM_MARKER}
## Observational Memory

{_CODEX_OM_FALLBACK_VERSION_MARKER}

Codex startup context is normally injected through hooks.

If this session does not already include sections titled `# Startup Profile` and `# Active Context`,
read these files before substantial work:

1. `~/.local/share/observational-memory/profile.md` — compact stable profile
2. `~/.local/share/observational-memory/active.md` — compact active context

If hooks are unavailable, that manual read is the fallback.

If this is a long-lived Codex session, Codex observations run every 15 minutes by default.
To adjust that interval, edit `~/.config/observational-memory/env` and set
`OM_CODEX_OBSERVER_INTERVAL_MINUTES` (for example: `OM_CODEX_OBSERVER_INTERVAL_MINUTES=5`).
You can run a manual checkpoint with `om observe --source codex`.

For deeper context when needed, consult:
- `~/.local/share/observational-memory/reflections.md`
- `~/.local/share/observational-memory/observations.md`
- `om search "<query>"`

These files are auto-maintained. Do not modify them directly.
{_CODEX_OM_MARKER}"""


def _build_codex_session_start_command() -> str:
    """Return the command string for the Codex SessionStart hook."""
    import shlex

    om_path = _find_om_path() or "om"
    return f"{shlex.quote(om_path)} context"


def _build_codex_checkpoint_command() -> str:
    """Return the command string for the Codex Stop hook."""
    import shlex

    om_path = _find_om_path() or "om"
    return f"{shlex.quote(om_path)} codex-checkpoint"


def _build_codex_checkpoint_worker_command(transcript: Path) -> list[str]:
    """Return argv for the detached Codex checkpoint worker process."""
    om_path = _find_om_path() or sys.argv[0] or "om"
    return [om_path, "codex-checkpoint-worker", "--transcript", str(transcript)]


def _command_invokes_om_subcommand(command: str, subcommand: str) -> bool:
    """Return True when *command* looks like `om <subcommand>`."""
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        return False

    return len(parts) == 2 and Path(parts[0]).name == "om" and parts[1] == subcommand


def _command_invokes_om_context(command: str) -> bool:
    """Return True when *command* looks like the OM SessionStart hook command."""
    return _command_invokes_om_subcommand(command, "context")


def _command_invokes_om_codex_checkpoint(command: str) -> bool:
    """Return True when *command* looks like the OM Codex Stop hook command."""
    return _command_invokes_om_subcommand(command, "codex-checkpoint")


def _hook_command_exists(command: str) -> bool:
    """Return True when the hook command's executable resolves locally."""
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        return False

    if not parts:
        return False

    executable = os.path.expanduser(parts[0])
    if "/" in executable:
        return Path(executable).exists()
    return shutil.which(executable) is not None


def _load_codex_hooks_payload(path: Path) -> dict:
    """Load hooks.json, validating the expected top-level shape."""
    import json

    if not path.exists():
        return {"hooks": {}}

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Failed to parse {path}: {e}") from e

    if not isinstance(payload, dict):
        raise click.ClickException(f"{path} must contain a JSON object.")

    hooks = payload.get("hooks")
    if hooks is None:
        payload["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise click.ClickException(f"{path} must contain a top-level 'hooks' object.")

    return payload


def _om_codex_session_start_group() -> dict:
    """Return the OM-managed Codex SessionStart hook group."""
    return {
        "matcher": _CODEX_SESSION_START_MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": _build_codex_session_start_command(),
                "timeout": 5,
                "statusMessage": _CODEX_SESSION_START_STATUS,
            }
        ],
    }


def _om_codex_stop_group() -> dict:
    """Return the OM-managed Codex Stop hook group."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": _build_codex_checkpoint_command(),
                "timeout": 5,
                "statusMessage": _CODEX_STOP_STATUS,
            }
        ]
    }


def _is_om_codex_session_start_group(group: object) -> bool:
    """Return True when *group* is the OM-managed Codex SessionStart hook group."""
    if not isinstance(group, dict):
        return False
    if group.get("matcher") != _CODEX_SESSION_START_MATCHER:
        return False

    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1:
        return False

    hook = hooks[0]
    if not isinstance(hook, dict):
        return False

    return (
        hook.get("type") == "command"
        and hook.get("statusMessage") == _CODEX_SESSION_START_STATUS
        and _command_invokes_om_context(hook.get("command", ""))
    )


def _is_om_codex_stop_group(group: object) -> bool:
    """Return True when *group* is the OM-managed Codex Stop hook group."""
    if not isinstance(group, dict):
        return False

    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1:
        return False

    hook = hooks[0]
    if not isinstance(hook, dict):
        return False

    return (
        hook.get("type") == "command"
        and hook.get("statusMessage") == _CODEX_STOP_STATUS
        and _command_invokes_om_codex_checkpoint(hook.get("command", ""))
    )


def _find_codex_session_start_hook(config: Config) -> tuple[dict | None, str | None]:
    """Return the installed OM SessionStart hook, or an error string if unreadable."""
    import json

    path = config.codex_hooks_path
    if not path.exists():
        return None, None

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, str(e)

    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return None, "top-level 'hooks' must be an object"

    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        return None, "'hooks.SessionStart' must be a list"

    for group in groups:
        if _is_om_codex_session_start_group(group):
            hook_list = group.get("hooks", [])
            if hook_list:
                hook = hook_list[0]
                if isinstance(hook, dict):
                    return hook, None
            return None, "invalid OM SessionStart hook group"

    return None, None


def _find_codex_stop_hook(config: Config) -> tuple[dict | None, str | None]:
    """Return the installed OM Stop hook, or an error string if unreadable."""
    import json

    path = config.codex_hooks_path
    if not path.exists():
        return None, None

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, str(e)

    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return None, "top-level 'hooks' must be an object"

    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        return None, "'hooks.Stop' must be a list"

    for group in groups:
        if _is_om_codex_stop_group(group):
            hook_list = group.get("hooks", [])
            if hook_list:
                hook = hook_list[0]
                if isinstance(hook, dict):
                    return hook, None
            return None, "invalid OM Stop hook group"

    return None, None


def _codex_agents_fallback_status(config: Config) -> str:
    """Return 'fallback', 'legacy', or 'missing' for the Codex AGENTS OM block."""
    if not config.codex_agents_md.exists():
        return "missing"

    content = config.codex_agents_md.read_text()
    if _CODEX_OM_MARKER not in content:
        return "missing"

    if _CODEX_OM_FALLBACK_VERSION_MARKER in content:
        return "fallback"

    return "legacy"


def _codex_hooks_feature_enabled(config: Config) -> tuple[bool | None, str | None]:
    """Return whether the Codex hooks feature is enabled, plus any read error."""
    import tomllib

    path = config.codex_config_path
    if not path.exists():
        return None, None

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        return None, str(e)

    features = data.get("features", {})
    if not isinstance(features, dict):
        return False, None

    return any(features.get(key) is True for key in _CODEX_HOOKS_FEATURE_FLAGS), None


def _enable_codex_hooks_feature(config: Config) -> None:
    """Ensure ~/.codex/config.toml enables Codex hooks across old and new flag names."""
    import re

    path = config.codex_config_path
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        lines = path.read_text().splitlines()
    else:
        lines = []

    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_res = {key: re.compile(rf"^\s*{re.escape(key)}\s*=") for key in _CODEX_HOOKS_FEATURE_FLAGS}
    dotted_key_res = {key: re.compile(rf"^\s*features\.{re.escape(key)}\s*=") for key in _CODEX_HOOKS_FEATURE_FLAGS}
    dotted_features_re = re.compile(r"^\s*features\.[A-Za-z0-9_-]+\s*=")
    changed = False
    feature_start = None
    feature_end = len(lines)
    dotted_feature_lines: list[int] = []

    for i, line in enumerate(lines):
        if dotted_features_re.match(line) and not line.lstrip().startswith("#"):
            dotted_feature_lines.append(i)

        match = section_re.match(line)
        if not match:
            continue
        if match.group(1).strip() == "features":
            feature_start = i
            feature_end = len(lines)
            for j in range(i + 1, len(lines)):
                if section_re.match(lines[j]):
                    feature_end = j
                    break
            break

    if feature_start is None and dotted_feature_lines:
        existing_lines = {}
        for i in dotted_feature_lines:
            for key, pattern in dotted_key_res.items():
                if pattern.match(lines[i]):
                    existing_lines[key] = i

        insert_at = dotted_feature_lines[-1] + 1
        for key in _CODEX_HOOKS_FEATURE_FLAGS:
            desired = f"features.{key} = true"
            existing_line = existing_lines.get(key)
            if existing_line is not None:
                if lines[existing_line].strip() != desired:
                    lines[existing_line] = desired
                    changed = True
            else:
                lines.insert(insert_at, desired)
                insert_at += 1
                changed = True
    elif feature_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", *[f"{key} = true" for key in _CODEX_HOOKS_FEATURE_FLAGS]])
        changed = True
    else:
        existing_lines = {}
        for j in range(feature_start + 1, feature_end):
            if lines[j].lstrip().startswith("#"):
                continue
            for key, pattern in key_res.items():
                if pattern.match(lines[j]):
                    existing_lines[key] = j

        insert_at = feature_end
        while insert_at > feature_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1

        for key in _CODEX_HOOKS_FEATURE_FLAGS:
            desired = f"{key} = true"
            existing_line = existing_lines.get(key)
            if existing_line is not None:
                if lines[existing_line].strip() != desired:
                    indent = lines[existing_line][: len(lines[existing_line]) - len(lines[existing_line].lstrip())]
                    lines[existing_line] = f"{indent}{desired}"
                    changed = True
            else:
                lines.insert(insert_at, desired)
                insert_at += 1
                changed = True

    if changed:
        path.write_text("\n".join(lines).rstrip() + "\n")

    if changed:
        click.echo(f"Enabled Codex hooks feature in {path}")
    else:
        click.echo(f"Codex hooks feature already enabled in {path}")


def _install_codex_session_start_hook(config: Config) -> None:
    """Install the OM-managed Codex SessionStart hook in hooks.json."""
    import json

    path = config.codex_hooks_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_codex_hooks_payload(path)
    hooks = payload.setdefault("hooks", {})

    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        raise click.ClickException(f"{path} has invalid 'hooks.SessionStart'; expected a list.")

    filtered = [group for group in groups if not _is_om_codex_session_start_group(group)]
    filtered.append(_om_codex_session_start_group())
    hooks["SessionStart"] = filtered

    path.write_text(json.dumps(payload, indent=2) + "\n")
    click.echo(f"Installed Codex SessionStart hook in {path}")


def _install_codex_stop_hook(config: Config) -> None:
    """Install the OM-managed Codex Stop hook in hooks.json."""
    import json

    path = config.codex_hooks_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_codex_hooks_payload(path)
    hooks = payload.setdefault("hooks", {})

    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        raise click.ClickException(f"{path} has invalid 'hooks.Stop'; expected a list.")

    filtered = [group for group in groups if not _is_om_codex_stop_group(group)]
    filtered.append(_om_codex_stop_group())
    hooks["Stop"] = filtered

    path.write_text(json.dumps(payload, indent=2) + "\n")
    click.echo(f"Installed Codex Stop hook in {path}")


def _uninstall_codex_session_start_hook(config: Config) -> None:
    """Remove the OM-managed Codex SessionStart hook from hooks.json."""
    import json

    path = config.codex_hooks_path
    if not path.exists():
        return

    payload = _load_codex_hooks_payload(path)
    hooks = payload.get("hooks", {})
    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        raise click.ClickException(f"{path} has invalid 'hooks.SessionStart'; expected a list.")

    filtered = [group for group in groups if not _is_om_codex_session_start_group(group)]
    if filtered:
        hooks["SessionStart"] = filtered
    else:
        hooks.pop("SessionStart", None)

    if hooks:
        path.write_text(json.dumps(payload, indent=2) + "\n")
        click.echo(f"Removed OM Codex SessionStart hook from {path}")
    else:
        remaining_top_level = {key: value for key, value in payload.items() if key != "hooks"}
        if remaining_top_level:
            payload["hooks"] = {}
            path.write_text(json.dumps(payload, indent=2) + "\n")
            click.echo(f"Removed OM Codex SessionStart hook from {path}")
        else:
            path.unlink()
            click.echo(f"Removed {path}")


def _uninstall_codex_stop_hook(config: Config) -> None:
    """Remove the OM-managed Codex Stop hook from hooks.json."""
    import json

    path = config.codex_hooks_path
    if not path.exists():
        return

    payload = _load_codex_hooks_payload(path)
    hooks = payload.get("hooks", {})
    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        raise click.ClickException(f"{path} has invalid 'hooks.Stop'; expected a list.")

    filtered = [group for group in groups if not _is_om_codex_stop_group(group)]
    if filtered:
        hooks["Stop"] = filtered
    else:
        hooks.pop("Stop", None)

    if hooks:
        path.write_text(json.dumps(payload, indent=2) + "\n")
        click.echo(f"Removed OM Codex Stop hook from {path}")
    else:
        remaining_top_level = {key: value for key, value in payload.items() if key != "hooks"}
        if remaining_top_level:
            payload["hooks"] = {}
            path.write_text(json.dumps(payload, indent=2) + "\n")
            click.echo(f"Removed OM Codex Stop hook from {path}")
        else:
            path.unlink()
            click.echo(f"Removed {path}")


def _uninstall_codex_hooks(config: Config) -> None:
    """Remove OM-managed Codex hooks from hooks.json in a single read-write pass."""
    import json

    path = config.codex_hooks_path
    if not path.exists():
        return

    payload = _load_codex_hooks_payload(path)
    hooks = payload.get("hooks", {})
    hook_specs = (
        ("SessionStart", _is_om_codex_session_start_group, "SessionStart"),
        ("Stop", _is_om_codex_stop_group, "Stop"),
    )
    removed: list[str] = []

    for event_name, predicate, label in hook_specs:
        groups = hooks.get(event_name, [])
        if not isinstance(groups, list):
            raise click.ClickException(f"{path} has invalid 'hooks.{event_name}'; expected a list.")

        filtered = [group for group in groups if not predicate(group)]
        if len(filtered) != len(groups):
            removed.append(label)

        if filtered:
            hooks[event_name] = filtered
        else:
            hooks.pop(event_name, None)

    if not removed:
        return

    if hooks:
        path.write_text(json.dumps(payload, indent=2) + "\n")
    else:
        remaining_top_level = {key: value for key, value in payload.items() if key != "hooks"}
        if remaining_top_level:
            payload["hooks"] = {}
            path.write_text(json.dumps(payload, indent=2) + "\n")
        else:
            path.unlink()
            click.echo(f"Removed {path}")
            return

    for label in removed:
        click.echo(f"Removed OM Codex {label} hook from {path}")


def _install_codex(config: Config) -> None:
    """Install Codex startup integration with hooks-first behavior."""
    import re

    _enable_codex_hooks_feature(config)
    _install_codex_session_start_hook(config)
    _install_codex_stop_hook(config)

    agents_md = config.codex_agents_md

    if agents_md.exists():
        content = agents_md.read_text()
        if _CODEX_OM_MARKER in content:
            pattern = rf"\n*{re.escape(_CODEX_OM_MARKER)}.*?{re.escape(_CODEX_OM_MARKER)}\n*"
            content = re.sub(pattern, "\n\n" + _CODEX_OM_BLOCK + "\n", content, flags=re.DOTALL)
            agents_md.write_text(content.strip() + "\n")
            click.echo(f"Updated observational memory instructions in {agents_md}")
            return
        content = content.rstrip() + "\n\n" + _CODEX_OM_BLOCK + "\n"
    else:
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        content = _CODEX_OM_BLOCK + "\n"

    agents_md.write_text(content)
    click.echo(f"Installed Codex AGENTS fallback in {agents_md}")


def _uninstall_codex(config: Config) -> None:
    """Remove OM Codex startup integration while preserving user hook settings."""
    _uninstall_codex_hooks(config)

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


def _count_codex_transcript_messages(transcript: Path) -> int:
    """Return the number of parsed Codex messages in a transcript."""
    from .transcripts.codex import parse_transcript

    if not transcript.exists():
        return 0

    try:
        return len(parse_transcript(transcript))
    except OSError:
        return 0


def _codex_checkpoint_lock_path(config: Config, transcript: Path) -> Path:
    """Return the lock directory path for one Codex transcript."""
    import hashlib

    digest = hashlib.sha256(str(transcript).encode("utf-8")).hexdigest()
    return config.codex_checkpoint_lock_dir / digest


def _codex_checkpoint_lock_stale_minutes(default: int = 60) -> int:
    raw_value = os.environ.get("OM_SESSION_OBSERVER_LOCK_STALE_MINUTES", str(default))
    try:
        stale_minutes = int(raw_value)
    except ValueError:
        return default

    return max(stale_minutes, 0)


def _acquire_codex_checkpoint_lock(config: Config, lock_path: Path) -> bool:
    """Acquire a best-effort mkdir lock for a Codex checkpoint worker."""
    stale_minutes = _codex_checkpoint_lock_stale_minutes()
    lock_dir = config.codex_checkpoint_lock_dir
    lock_dir.mkdir(parents=True, exist_ok=True)

    if stale_minutes > 0:
        for entry in lock_dir.iterdir():
            if not entry.is_dir():
                continue
            age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - entry.stat().st_mtime)
            if age_seconds > stale_minutes * 60:
                shutil.rmtree(entry, ignore_errors=True)

    try:
        lock_path.mkdir()
        return True
    except FileExistsError:
        if stale_minutes <= 0 or not lock_path.exists():
            return False

        age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime)
        if age_seconds <= stale_minutes * 60:
            return False

        shutil.rmtree(lock_path, ignore_errors=True)
        try:
            lock_path.mkdir()
            return True
        except FileExistsError:
            return False


def _release_codex_checkpoint_lock(lock_path: Path) -> None:
    """Release a Codex checkpoint mkdir lock if it exists."""
    shutil.rmtree(lock_path, ignore_errors=True)


def _load_codex_checkpoint_state(config: Config) -> dict[str, dict]:
    """Load Codex checkpoint hook state, tolerating missing or invalid files."""
    import json

    path = config.codex_checkpoint_state_path
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _update_codex_checkpoint_state(
    config: Config,
    transcript: Path,
    *,
    message_count: int,
    status: str,
) -> None:
    """Persist the latest Codex checkpoint hook state for one transcript."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        fcntl = None

    import json
    import tempfile

    path = config.codex_checkpoint_state_path
    lock_path = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        try:
            state = _load_codex_checkpoint_state(config)
            state[str(transcript)] = {
                "last_observed": int(datetime.now(timezone.utc).timestamp()),
                "message_count": max(message_count, 0),
                "status": status,
            }

            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
                json.dump(state, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _resolve_scheduler_mode(requested: str, cron_compat: bool | None, platform: str | None = None) -> str:
    """Resolve the scheduler mode, preserving legacy --cron/--no-cron behavior."""
    active_platform = platform or sys.platform
    requested = requested.lower()

    if cron_compat is not None:
        compat_mode = "cron" if cron_compat else "none"
        if requested != "auto" and requested != compat_mode:
            raise click.ClickException("--cron/--no-cron conflicts with --scheduler.")
        requested = compat_mode

    if requested == "auto":
        return "launchd" if active_platform == "darwin" else "cron"

    if requested == "launchd" and active_platform != "darwin":
        raise click.ClickException("--scheduler launchd is only supported on macOS.")

    return requested


def _launchd_domain_target() -> str:
    """Return the per-user GUI domain target for launchctl."""
    return f"gui/{os.getuid()}"


def _launchd_service_target(label: str) -> str:
    """Return the fully qualified launchctl service target."""
    return f"{_launchd_domain_target()}/{label}"


def _launchd_job_specs(config: Config, targets: str, om_path: str | None = None) -> list[dict[str, object]]:
    """Return OM-managed launchd job specs for the selected install targets."""
    resolved_om_path = str(Path(om_path).expanduser()) if om_path else None
    specs: list[dict[str, object]] = []

    if targets in ("codex", "both"):
        specs.append(
            {
                "key": "codex",
                "label": config.CODEX_OBSERVE_LAUNCHD_LABEL,
                "plist_path": config.codex_observe_launchd_plist_path,
                "argv": [resolved_om_path, "observe", "--source", "codex"] if resolved_om_path else [],
                "run_at_load": True,
                "start_interval": _codex_observer_interval_minutes() * 60,
                "stdout_path": config.codex_observe_launchd_stdout_path,
                "stderr_path": config.codex_observe_launchd_stderr_path,
            }
        )

    specs.extend(
        [
            {
                "key": "claude-memory",
                "label": config.AUTO_MEMORY_LAUNCHD_LABEL,
                "plist_path": config.auto_memory_launchd_plist_path,
                "argv": [resolved_om_path, "observe", "--source", "claude-memory"] if resolved_om_path else [],
                "run_at_load": True,
                "start_interval": 3600,
                "stdout_path": config.auto_memory_launchd_stdout_path,
                "stderr_path": config.auto_memory_launchd_stderr_path,
            },
            {
                "key": "reflect",
                "label": config.REFLECT_LAUNCHD_LABEL,
                "plist_path": config.reflect_launchd_plist_path,
                "argv": [resolved_om_path, "reflect"] if resolved_om_path else [],
                "run_at_load": False,
                "start_calendar_interval": {"Hour": 4, "Minute": 0},
                "stdout_path": config.reflect_launchd_stdout_path,
                "stderr_path": config.reflect_launchd_stderr_path,
            },
        ]
    )
    return specs


def _launchd_plist_payload(spec: dict[str, object]) -> dict[str, object]:
    """Build a launchd plist payload from one OM job spec."""
    payload: dict[str, object] = {
        "Label": spec["label"],
        "ProgramArguments": spec["argv"],
        "StandardOutPath": str(spec["stdout_path"]),
        "StandardErrorPath": str(spec["stderr_path"]),
    }

    if spec.get("run_at_load"):
        payload["RunAtLoad"] = True

    start_interval = spec.get("start_interval")
    if isinstance(start_interval, int):
        payload["StartInterval"] = start_interval

    start_calendar_interval = spec.get("start_calendar_interval")
    if isinstance(start_calendar_interval, dict):
        payload["StartCalendarInterval"] = start_calendar_interval

    return payload


def _write_launchd_plist(path: Path, payload: dict[str, object]) -> None:
    """Write a launchd plist payload as XML."""
    import plistlib

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))


def _launchctl_bootout(label: str) -> None:
    """Best-effort bootout for an OM launchd job."""
    import subprocess

    subprocess.run(
        ["launchctl", "bootout", _launchd_service_target(label)],
        capture_output=True,
        text=True,
    )


def _launchctl_bootstrap(plist_path: Path) -> None:
    """Bootstrap one OM launchd plist into the current user's GUI domain."""
    import subprocess

    result = subprocess.run(
        ["launchctl", "bootstrap", _launchd_domain_target(), str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise click.ClickException(f"Failed to bootstrap launchd agent {plist_path.name}: {detail}")


def _launchctl_service_loaded(label: str, timeout: int = _SCHEDULER_COMMAND_TIMEOUT_SECONDS) -> tuple[bool, str | None]:
    """Return whether one OM launchd job is currently loaded."""
    import subprocess

    try:
        result = subprocess.run(
            ["launchctl", "print", _launchd_service_target(label)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "launchctl not available"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"

    if result.returncode == 0:
        return True, None

    detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").lower()
    if "could not find service" in detail or "service could not be found" in detail:
        return False, None
    return False, detail


def _launchd_job_statuses(config: Config, targets: str = "both") -> list[dict[str, object]]:
    """Return installed/loaded state for OM-managed launchd jobs."""
    if sys.platform != "darwin":
        return []

    jobs: list[dict[str, object]] = []
    for spec in _launchd_job_specs(config, targets):
        label = spec.get("label")
        plist_path = spec.get("plist_path")
        key = spec.get("key")
        if not isinstance(label, str) or not isinstance(plist_path, Path):
            continue
        loaded, error = _launchctl_service_loaded(label)
        jobs.append(
            {
                "key": key or label,
                "label": label,
                "plist_path": plist_path,
                "installed": plist_path.exists(),
                "loaded": loaded,
                "error": error,
            }
        )
    return jobs


def _install_launchd(config: Config, targets: str) -> None:
    """Install OM-managed LaunchAgents on macOS."""
    if sys.platform != "darwin":
        raise click.ClickException("launchd installation is only supported on macOS.")

    om_path = _find_om_path()
    if not om_path:
        raise click.ClickException("Could not resolve an absolute 'om' path for launchd jobs.")

    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.scheduler_log_dir.mkdir(parents=True, exist_ok=True)

    specs = _launchd_job_specs(config, targets, om_path=om_path)
    for spec in specs:
        plist_path = spec["plist_path"]
        label = spec["label"]
        if not isinstance(plist_path, Path) or not isinstance(label, str):
            continue
        _write_launchd_plist(plist_path, _launchd_plist_payload(spec))
        _launchctl_bootout(label)
        _launchctl_bootstrap(plist_path)

    click.echo(f"Installed {len(specs)} launchd job(s)")


def _uninstall_launchd(config: Config, targets: str = "both") -> None:
    """Remove OM-managed LaunchAgents from macOS."""
    if sys.platform != "darwin":
        return

    specs = _launchd_job_specs(config, targets)
    removed = False

    for spec in specs:
        plist_path = spec["plist_path"]
        label = spec["label"]
        if not isinstance(plist_path, Path) or not isinstance(label, str):
            continue
        _launchctl_bootout(label)
        if plist_path.exists():
            plist_path.unlink()
            removed = True

    if removed:
        click.echo("Removed launchd jobs")


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


def _cron_job_key(line: str) -> str | None:
    """Return the OM cron job key for a crontab line, if any."""
    if "om observe --source codex" in line:
        return "codex"
    if "om observe --source claude-memory" in line:
        return "claude-memory"
    if "om reflect" in line:
        return "reflect"
    return None


def _cron_job_keys_for_targets(targets: str) -> set[str]:
    """Return the OM cron job keys scoped to one install target selection."""
    keys = {"claude-memory", "reflect"}
    if targets in ("codex", "both"):
        keys.add("codex")
    return keys


def _extract_crontab_lines(lines: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split crontab lines into non-OM lines and OM-managed job lines."""
    preserved: list[str] = []
    om_jobs: dict[str, str] = {}

    for line in lines:
        stripped = line.strip()
        if stripped in {"# --- observational-memory ---", "# --- end observational-memory ---"}:
            continue

        job_key = _cron_job_key(line)
        if job_key is not None:
            om_jobs[job_key] = line
            continue

        preserved.append(line)

    return preserved, om_jobs


def _render_crontab_lines(preserved: list[str], om_jobs: dict[str, str]) -> list[str]:
    """Render preserved and OM job lines back into a crontab."""
    lines = list(preserved)
    ordered_jobs = [om_jobs[key] for key in ("codex", "claude-memory", "reflect") if key in om_jobs]
    if ordered_jobs:
        lines.append("# --- observational-memory ---")
        lines.extend(ordered_jobs)
        lines.append("# --- end observational-memory ---")
    return lines


def _subprocess_detail(result: object) -> str:
    """Return the most useful stderr/stdout snippet from a subprocess result."""
    stderr = getattr(result, "stderr", "") or ""
    stdout = getattr(result, "stdout", "") or ""
    return stderr.strip() or stdout.strip() or f"exit {getattr(result, 'returncode', 'unknown')}"


def _read_crontab(timeout: int = _SCHEDULER_COMMAND_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
    """Return the current crontab contents, treating 'no crontab' as empty."""
    import subprocess

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "crontab not available"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"

    if result.returncode == 0:
        return result.stdout, None

    detail = _subprocess_detail(result)
    if "no crontab for" in detail.lower():
        return "", None
    return None, detail


def _write_crontab(contents: str, timeout: int = _SCHEDULER_COMMAND_TIMEOUT_SECONDS) -> str | None:
    """Write a new crontab payload and return an error string on failure."""
    import subprocess

    try:
        result = subprocess.run(
            ["crontab", "-"],
            input=contents,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "crontab not available"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"

    if result.returncode == 0:
        return None
    return _subprocess_detail(result)


def _om_cron_jobs(timeout: int = _SCHEDULER_COMMAND_TIMEOUT_SECONDS) -> tuple[dict[str, str] | None, str | None]:
    """Return OM-managed cron jobs keyed by logical job name."""
    contents, error = _read_crontab(timeout=timeout)
    if error is not None or contents is None:
        return None, error

    _, om_jobs = _extract_crontab_lines(contents.splitlines())
    return om_jobs, None


def _desired_cron_jobs(config: Config, targets: str) -> dict[str, str]:
    """Return the desired OM cron job lines keyed by logical job name."""
    om_path = _find_om_path()
    if not om_path:
        click.echo("Warning: 'om' not found in PATH. Cron jobs will use 'om' — make sure it's installed.")
        om_path = "om"

    env_file = config.env_file
    if env_file.exists():
        prefix = f". {env_file} && "
    else:
        prefix = ""

    jobs = {
        "claude-memory": f"0 * * * * {prefix}{om_path} observe --source claude-memory 2>/dev/null",
        "reflect": f"0 4 * * * {prefix}{om_path} reflect 2>/dev/null",
    }

    if targets in ("codex", "both"):
        codex_interval = _cron_every_minutes(_codex_observer_interval_minutes())
        jobs["codex"] = f"{codex_interval} * * * * {prefix}{om_path} observe --source codex 2>/dev/null"

    return jobs


def _install_cron(config: Config, targets: str) -> None:
    """Add cron jobs for observer and reflector."""
    existing, read_error = _read_crontab()
    if read_error is not None or existing is None:
        click.echo(f"Warning: Failed to read crontab: {read_error}")
        return

    preserved, existing_jobs = _extract_crontab_lines(existing.splitlines())
    target_keys = _cron_job_keys_for_targets(targets)
    desired_jobs = _desired_cron_jobs(config, targets)
    merged_jobs = {key: line for key, line in existing_jobs.items() if key not in target_keys}
    merged_jobs.update(desired_jobs)

    new_crontab = "\n".join(_render_crontab_lines(preserved, merged_jobs)) + "\n"

    write_error = _write_crontab(new_crontab)
    if write_error is None:
        click.echo(f"Installed {len(desired_jobs)} cron job(s)")
    else:
        click.echo(f"Warning: Failed to install cron jobs: {write_error}")


def _uninstall_cron(targets: str = "both") -> None:
    """Remove observational memory cron jobs."""
    existing, read_error = _read_crontab()
    if read_error is not None or existing is None:
        click.echo(f"Warning: Failed to read crontab: {read_error}")
        return

    preserved, existing_jobs = _extract_crontab_lines(existing.splitlines())
    target_keys = _cron_job_keys_for_targets(targets)
    if not set(existing_jobs).intersection(target_keys):
        return

    remaining_jobs = {key: line for key, line in existing_jobs.items() if key not in target_keys}
    rendered = _render_crontab_lines(preserved, remaining_jobs)
    new_crontab = "\n".join(rendered) + "\n" if rendered else ""
    write_error = _write_crontab(new_crontab)
    if write_error is None:
        click.echo("Removed cron jobs")
    else:
        click.echo(f"Warning: Failed to remove cron jobs: {write_error}")


def _find_om_path() -> str | None:
    """Find the absolute path to the 'om' command."""
    import shutil

    return shutil.which("om")
