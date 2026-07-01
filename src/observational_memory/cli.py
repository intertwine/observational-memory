"""CLI entry points: om observe, om reflect, om backfill, om search, om context, om install, om status, om doctor."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import click

from . import __version__
from .config import Config

_OBSERVE_SOURCES = ["claude", "codex", "opencode", "kimi", "grok", "hermes", "cowork", "claude-memory", "all"]
_OBSERVER_WORKER_SOURCES = _OBSERVE_SOURCES
_T = TypeVar("_T")


class ObserverWorkerBusy(RuntimeError):
    """Raised when another background observer worker already owns the global slot."""


class ObserverWorkerTimeout(BaseException):
    """Raised when background observer work exceeds its wall-clock budget."""


@click.group()
@click.version_option(__version__, prog_name="om")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Observational Memory — shared memory for Claude Code, Codex CLI, OpenCode, Kimi Code CLI, and Hermes Agent."""
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
        observe_all_cowork,
        observe_all_grok,
        observe_all_hermes,
        observe_all_kimi,
        observe_all_opencode,
        observe_auto_memory,
        observe_claude_transcript,
        observe_codex_transcript,
        observe_cowork_transcript,
        observe_grok_transcript,
        observe_hermes_transcript,
        observe_kimi_transcript,
        observe_opencode_transcript,
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
        elif transcript_source == "kimi":
            result = observe_kimi_transcript(transcript, config, dry_run)
        elif transcript_source == "cowork":
            result = observe_cowork_transcript(transcript, config, dry_run)
        elif transcript_source == "grok":
            result = observe_grok_transcript(transcript, config, dry_run)
        elif transcript_source == "opencode":
            result = observe_opencode_transcript(transcript, config, dry_run)
        elif transcript_source == "claude-memory":
            raise click.ClickException("--transcript does not support --source claude-memory.")
        else:
            raise click.ClickException(
                "Could not detect transcript source. "
                "Pass --source claude, --source codex, --source opencode, "
                "--source kimi, --source grok, --source hermes, or --source cowork."
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

    if source in ("opencode", "all"):
        click.echo("Scanning OpenCode sessions...")
        results.extend(observe_all_opencode(config, dry_run))

    if source in ("hermes", "all"):
        click.echo("Scanning Hermes sessions...")
        results.extend(observe_all_hermes(config=config, dry_run=dry_run))

    if source in ("kimi", "all"):
        click.echo("Scanning Kimi Code events...")
        results.extend(observe_all_kimi(config=config, dry_run=dry_run))

    if source in ("cowork", "all"):
        click.echo("Scanning Cowork sessions...")
        results.extend(observe_all_cowork(config, dry_run))

    if source in ("grok", "all"):
        click.echo("Scanning Grok sessions...")
        results.extend(observe_all_grok(config, dry_run))

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
        transcript.relative_to(config.opencode_events_dir)
        return "opencode"
    except ValueError:
        pass

    try:
        transcript.relative_to(config.hermes_sessions_dir)
        return "hermes"
    except ValueError:
        pass

    if transcript == config.kimi_om_events_path:
        return "kimi"

    try:
        transcript.relative_to(config.cowork_sessions_dir)
        return "cowork"
    except ValueError:
        pass

    return None


def _observer_worker_timeout_seconds(default: int = 300) -> int:
    raw_value = os.environ.get("OM_OBSERVER_WORKER_TIMEOUT_SECONDS", str(default))
    try:
        seconds = int(raw_value)
    except ValueError:
        click.echo(
            f"Warning: invalid OM_OBSERVER_WORKER_TIMEOUT_SECONDS={raw_value!r}; using default {default}.",
            err=True,
        )
        return default
    if seconds < 0:
        click.echo(
            f"Warning: OM_OBSERVER_WORKER_TIMEOUT_SECONDS must be >=0; using default {default}.",
            err=True,
        )
        return default
    return seconds


def _observer_worker_lock_stale_seconds(timeout_seconds: int | None = None) -> int:
    timeout = _observer_worker_timeout_seconds() if timeout_seconds is None else timeout_seconds
    default = max(timeout + 60, 60) if timeout > 0 else 600
    raw_value = os.environ.get("OM_OBSERVER_WORKER_LOCK_STALE_SECONDS", str(default))
    try:
        seconds = int(raw_value)
    except ValueError:
        click.echo(
            f"Warning: invalid OM_OBSERVER_WORKER_LOCK_STALE_SECONDS={raw_value!r}; using default {default}.",
            err=True,
        )
        return default
    if seconds < 0:
        click.echo(
            f"Warning: OM_OBSERVER_WORKER_LOCK_STALE_SECONDS must be >=0; using default {default}.",
            err=True,
        )
        return default
    return seconds


def _observer_worker_lock_path(config: Config) -> Path:
    return config.memory_dir / ".observer-worker.lock"


@contextmanager
def _observer_worker_slot(config: Config, *, timeout_seconds: int | None = None) -> Iterator[None]:
    from .sync.atomic import DirectoryLock

    timeout = _observer_worker_timeout_seconds() if timeout_seconds is None else timeout_seconds
    lock = DirectoryLock(
        _observer_worker_lock_path(config),
        timeout_seconds=0,
        stale_seconds=_observer_worker_lock_stale_seconds(timeout),
        reclaim_alive_stale=False,
    )
    try:
        lock.acquire()
    except TimeoutError as e:
        raise ObserverWorkerBusy("another background observer worker is already running") from e

    try:
        yield
    finally:
        lock.release()


def _run_with_wall_timeout(fn: Callable[[], _T], timeout_seconds: int) -> _T:
    if timeout_seconds <= 0 or sys.platform == "win32":
        return fn()

    try:
        import signal
    except ImportError:
        return fn()

    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return fn()

    def _timeout_handler(signum, frame):
        raise ObserverWorkerTimeout(f"background observer exceeded {timeout_seconds}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def _observer_process_entry(result_queue, fn: Callable[..., _T], args: tuple, kwargs: dict) -> None:
    try:
        result_queue.put(("ok", fn(*args, **kwargs)))
    except BaseException as e:
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


def _run_with_process_timeout(fn: Callable[..., _T], timeout_seconds: int, *args, **kwargs) -> _T:
    if timeout_seconds <= 0:
        return fn(*args, **kwargs)

    import multiprocessing
    import queue

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_observer_process_entry, args=(result_queue, fn, args, kwargs))
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(1)
        raise ObserverWorkerTimeout(f"background observer exceeded {timeout_seconds}s")

    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty:
        if process.exitcode == 0:
            return None  # type: ignore[return-value]
        raise RuntimeError(f"background observer child exited with status {process.exitcode}")

    if status == "ok":
        return payload
    raise RuntimeError(f"background observer child failed: {payload}")


def _run_bounded_observer_work(config: Config, fn: Callable[[], _T]) -> _T:
    timeout = _observer_worker_timeout_seconds()
    with _observer_worker_slot(config, timeout_seconds=timeout):
        if sys.platform == "win32":
            raise RuntimeError("Windows bounded observer work must use a top-level callable")
        return _run_with_wall_timeout(fn, timeout)


def _run_bounded_observer_call(config: Config, fn: Callable[..., _T], *args, **kwargs) -> _T:
    timeout = _observer_worker_timeout_seconds()
    with _observer_worker_slot(config, timeout_seconds=timeout):
        if sys.platform == "win32":
            return _run_with_process_timeout(fn, timeout, *args, **kwargs)
        return _run_with_wall_timeout(lambda: fn(*args, **kwargs), timeout)


def _run_observe_worker_source(config: Config, source: str) -> int:
    from .observe import (
        observe_all_claude,
        observe_all_codex,
        observe_all_cowork,
        observe_all_grok,
        observe_all_hermes,
        observe_all_kimi,
        observe_all_opencode,
        observe_auto_memory,
    )

    processed = 0
    if source in ("claude", "all"):
        processed += len(observe_all_claude(config, dry_run=False))
    if source in ("codex", "all"):
        processed += len(observe_all_codex(config, dry_run=False))
    if source in ("opencode", "all"):
        processed += len(observe_all_opencode(config, dry_run=False))
    if source in ("hermes", "all"):
        processed += len(observe_all_hermes(config=config, dry_run=False))
    if source in ("kimi", "all"):
        processed += len(observe_all_kimi(config=config, dry_run=False))
    if source in ("cowork", "all"):
        processed += len(observe_all_cowork(config, dry_run=False))
    if source in ("grok", "all"):
        processed += len(observe_all_grok(config, dry_run=False))
    if source in ("claude-memory", "all"):
        changed, deleted = observe_auto_memory(config, dry_run=False)
        processed += len(changed) + len(deleted)
    _maybe_run_reflector_catchup(config)
    return processed


@cli.command(hidden=True, name="observe-worker")
@click.option(
    "--source",
    type=click.Choice(_OBSERVER_WORKER_SOURCES),
    required=True,
    help="Agent or memory source to observe inside the bounded background worker lane.",
)
@click.pass_context
def observe_worker(ctx: click.Context, source: str) -> None:
    """Run background observation with global concurrency and wall-time guards."""
    config = ctx.obj["config"]
    try:
        processed = _run_bounded_observer_call(config, _run_observe_worker_source, config, source)
    except ObserverWorkerBusy:
        click.echo("Observer worker skipped: another background observer is already running.")
        return
    except ObserverWorkerTimeout as e:
        click.echo(f"Observer worker timed out: {e}", err=True)
        return

    if processed:
        click.echo(f"Observer worker processed {processed} item(s).")
    else:
        click.echo("Observer worker found no new work.")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print reflections without writing")
@click.option(
    "--async",
    "async_mode",
    is_flag=True,
    help="Submit an offline OpenAI Batch job (API-key 'openai' provider) and exit; apply later with `om jobs poll`",
)
@click.option(
    "--check-conflicts",
    "check_conflicts",
    is_flag=True,
    help="Also diff prior vs new reflections for silently-changed high-stakes facts (read-only advisory)",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable conflict report (implies --check-conflicts)")
@click.pass_context
def reflect(ctx: click.Context, dry_run: bool, async_mode: bool, check_conflicts: bool, as_json: bool) -> None:
    """Run the reflector to condense observations into long-term memory."""
    from .reflect import run_reflector

    config = ctx.obj["config"]
    check_conflicts = check_conflicts or as_json

    # --json keeps stdout pure for the machine report, so reflector chatter goes
    # to stderr in that mode.
    chatter_err = as_json

    # Async is opt-in via --async (explicit) or OM_OPENAI_ASYNC_MODE=batch (env).
    # dry-run always runs synchronously (there's nothing to defer). The conflict
    # diff needs the new document in hand now, so it also forces a synchronous run.
    env_async = config.openai_async_mode.strip().lower() == "batch"
    if not dry_run and not check_conflicts and (async_mode or env_async):
        _reflect_async(config, explicit=async_mode)
        return
    if check_conflicts and async_mode:
        # The diff needs the new document in hand, so an explicit --async is run
        # synchronously here. Say so rather than swallowing the flag silently.
        click.echo("Note: --check-conflicts requires a synchronous run; ignoring --async.", err=True)

    # Capture the prior on-disk reflections BEFORE the reflector writes, so the
    # conflict diff compares the genuine pre-reflect state against the new doc.
    prior_reflections = ""
    if check_conflicts and config.reflections_path.exists():
        prior_reflections = config.reflections_path.read_text()

    click.echo("Running reflector...", err=chatter_err)
    result = run_reflector(config, dry_run)
    if result:
        click.echo(f"Reflections updated ({len(result)} chars)", err=chatter_err)
        if dry_run and not as_json:
            click.echo(result)
    else:
        click.echo("No observations to reflect on.", err=chatter_err)

    if check_conflicts:
        from .reflection_metadata import diff_reflection_conflicts

        conflicts = diff_reflection_conflicts(prior_reflections, result or "")
        _emit_conflict_report(conflicts, as_json=as_json)


def _emit_conflict_report(conflicts: list, as_json: bool) -> None:
    """Emit the read-only conflict advisory: a human summary to stderr, the full
    report to a throwaway temp file (only when conflicts exist), and machine JSON
    to stdout when requested. Never writes durable memory; always exit 0."""
    report_path = _write_conflict_report(conflicts) if conflicts else None

    if as_json:
        click.echo(
            json.dumps(
                {
                    "conflicts": [c.to_dict() for c in conflicts],
                    "report_path": str(report_path) if report_path else None,
                },
                indent=2,
            )
        )

    if not conflicts:
        click.echo("No high-stakes reflection conflicts (prior vs new).", err=True)
        return

    plural = "s" if len(conflicts) != 1 else ""
    click.echo(f"⚠ {len(conflicts)} high-stakes reflection conflict{plural} (prior vs new):", err=True)
    for conflict in conflicts:
        click.echo(f"  [{conflict.actionability}] {conflict.section} · {conflict.kind}", err=True)
        for entry in conflict.entries:
            click.echo(f"    {entry['side']:>5}: {entry['text']}", err=True)
    if report_path:
        click.echo(f"Full report: {report_path}", err=True)


def _write_conflict_report(conflicts: list) -> Path:
    """Write the full conflict report to a throwaway temp file and return its
    path. The artifact is intentionally outside the memory store — never durable,
    never synced. A single deterministic filename is reused (overwritten) each
    run so repeated reflects don't litter the temp directory."""
    import tempfile

    lines = ["# Reflection conflict report", "", f"{len(conflicts)} high-stakes conflict(s) between prior and new.", ""]
    for conflict in conflicts:
        lines.append(f"## {conflict.section} · {conflict.kind} [{conflict.actionability}]")
        for entry in conflict.entries:
            lines.append(f"- **{entry['side']}** ({entry.get('signal', '?')}): {entry['text']}")
        lines.append("")
    report_path = Path(tempfile.gettempdir()) / "om-conflicts-latest.md"
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path


def _valid_backup_reason(reason: str) -> bool:
    import re

    return bool(re.match(r"^[a-z0-9-]+$", reason))


def _snapshot_to_dict(snapshot) -> dict:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "reason": snapshot.reason,
        "created_at": snapshot.created_at,
        "files": list(snapshot.files),
        "bytes_total": snapshot.bytes_total,
        "path": str(snapshot.path),
    }


def _echo_snapshot_list(snapshots) -> None:
    if not snapshots:
        click.echo("No snapshots yet.")
        return
    for snapshot in snapshots:
        click.echo(f"{snapshot.snapshot_id}\t{snapshot.reason}\t{snapshot.created_at}\t{snapshot.bytes_total} bytes")


@cli.command()
@click.option("--reason", default="manual", help="Snapshot label (lowercase letters, digits, hyphens)")
@click.option("--list", "list_only", is_flag=True, help="List existing snapshots instead of creating one")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.pass_context
def backup(ctx: click.Context, reason: str, list_only: bool, as_json: bool) -> None:
    """Create an on-demand snapshot of memory, or list existing snapshots."""
    from .backup import create_snapshot, list_snapshots

    config = ctx.obj["config"]

    if list_only:
        snapshots = list_snapshots(config)
        if as_json:
            click.echo(json.dumps([_snapshot_to_dict(s) for s in snapshots], indent=2))
        else:
            _echo_snapshot_list(snapshots)
        return

    if not _valid_backup_reason(reason):
        raise click.ClickException("--reason must contain only lowercase letters, digits, and hyphens.")

    try:
        info = create_snapshot(config, reason=reason)
    except Exception as exc:  # noqa: BLE001 — surface as a clean CLI error
        raise click.ClickException(f"Backup failed: {exc}") from exc

    if info is None:
        click.echo("Nothing to back up (no memory files yet or backups disabled).")
        return

    if as_json:
        click.echo(json.dumps(_snapshot_to_dict(info), indent=2))
    else:
        click.echo(f"Snapshot created: {info.snapshot_id} ({len(info.files)} files, {info.bytes_total} bytes)")
        click.echo(str(info.path))


@cli.command()
@click.argument("snapshot_id", required=False)
@click.option("--latest", is_flag=True, help="Restore the newest snapshot")
@click.option("--list", "list_only", is_flag=True, help="List snapshots and exit")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option(
    "--no-safety-snapshot",
    is_flag=True,
    help="Do not snapshot current state before restoring (discouraged)",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.pass_context
def restore(
    ctx: click.Context,
    snapshot_id: str | None,
    latest: bool,
    list_only: bool,
    yes: bool,
    no_safety_snapshot: bool,
    as_json: bool,
) -> None:
    """Restore memory files from a snapshot (byte-faithful)."""
    from .backup import (
        RestoreFailedError,
        RestorePartialError,
        list_snapshots,
        resolve_snapshot,
        restore_snapshot,
    )
    from .reflect import _reindex_if_enabled

    config = ctx.obj["config"]

    if list_only:
        snapshots = list_snapshots(config)
        if as_json:
            click.echo(json.dumps([_snapshot_to_dict(s) for s in snapshots], indent=2))
        else:
            _echo_snapshot_list(snapshots)
        return

    # Restore overwrites live memory — never restore implicitly. Require an
    # explicit snapshot id or --latest; otherwise show the list and stop.
    if snapshot_id is None and not latest:
        click.echo("Choose a snapshot to restore (pass a SNAPSHOT_ID or --latest):")
        _echo_snapshot_list(list_snapshots(config))
        return

    if latest and snapshot_id is not None:
        raise click.ClickException("Pass either a SNAPSHOT_ID or --latest, not both.")

    selector = "latest" if latest else snapshot_id
    try:
        snapshot = resolve_snapshot(config, selector)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not yes and not click.confirm(f"Overwrite live memory from {snapshot.snapshot_id}?"):
        click.echo("Aborted; memory unchanged.")
        return

    try:
        safety = restore_snapshot(
            config,
            snapshot,
            make_safety_snapshot=not no_safety_snapshot,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    except RestoreFailedError as exc:
        # Mid-restore failure that was automatically rolled back: live memory is
        # back to its pre-restore state. Report as a clean one-line CLI error.
        raise click.ClickException(str(exc)) from exc
    except RestorePartialError as exc:
        # Mid-restore failure that could NOT be rolled back: live memory may be
        # half-restored. The message already names the recovery command.
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        # Any other I/O failure escaping restore_snapshot: never leak a traceback.
        raise click.ClickException(f"Restore of {snapshot.snapshot_id} failed ({type(exc).__name__}: {exc}).") from exc

    # Keep the search index consistent with the restored Markdown (and any
    # regenerated profile.md/active.md for subset snapshots).
    _reindex_if_enabled(config)

    safety_id = safety.snapshot_id if (not no_safety_snapshot and safety is not snapshot) else None
    if as_json:
        click.echo(
            json.dumps(
                {
                    "restored": snapshot.snapshot_id,
                    "files": list(snapshot.files),
                    "safety_snapshot": safety_id,
                },
                indent=2,
            )
        )
    else:
        suffix = f"; safety snapshot: {safety_id}" if safety_id else ""
        click.echo(f"Restored {len(snapshot.files)} files from {snapshot.snapshot_id}{suffix}")


def _run_reflector_sync(config: Config) -> None:
    from .reflect import run_reflector
    from .usage.budgets import BudgetExceededError

    # The synchronous LLM path raises a retry-exhausted RuntimeError from
    # llm.compress (after _is_retryable's 429/5xx retries are spent); surface it
    # as a clean CLI error instead of a raw traceback. BudgetExceededError also
    # subclasses RuntimeError but is terminal with its own dedicated wording, so
    # let it propagate (_reflect_async handles it on both submit and degrade paths).
    try:
        result = run_reflector(config)
    except BudgetExceededError:
        raise
    except RuntimeError as exc:
        raise click.ClickException(f"Reflection failed: {exc}") from exc
    click.echo(f"Reflections updated ({len(result)} chars)" if result else "No observations to reflect on.")


def _run_reflector_sync_degrade(config: Config) -> None:
    """Run the synchronous reflector on the async->sync degrade path.

    A BudgetExceededError raised by the synchronous llm.compress pre-call gate
    is terminal (the same cap applies sync or async), so surface it with the
    dedicated budget wording — matching how _reflect_async handles a budget
    block on the submit path — rather than the generic "Reflection failed".
    """
    from .usage.budgets import BudgetExceededError

    try:
        _run_reflector_sync(config)
    except BudgetExceededError as exc:
        raise click.ClickException(str(exc)) from exc


def _openai_batch_submit_message(exc: Exception) -> str:
    """Map an OpenAI submit error to a one-line, actionable CLI message.

    Billing/quota failures (``billing_hard_limit_reached`` / ``insufficient_quota``)
    get a plan/limits hint; everything else falls back to the provider message.
    """
    code = getattr(exc, "code", None) or ""
    if code in ("billing_hard_limit_reached", "insufficient_quota"):
        return (
            "OpenAI Batch submission failed: billing hard limit reached. "
            "Check your OpenAI plan/limits (project-scoped keys have per-project spend caps)."
        )
    # Prefer the clean inner provider message. The SDK puts the parsed error
    # object on exc.body (a mapping with "message"), while exc.message is the
    # verbose "Error code: 400 - {'error': {...}}" envelope dump — not actionable.
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping) and body.get("message"):
        detail = str(body["message"])
    else:
        detail = str(getattr(exc, "code", None) or exc) or "provider error"
    return f"OpenAI Batch submission failed: {detail}"


def _reflect_async(config: Config, explicit: bool) -> None:
    """Submit a reflection as an OpenAI Batch job.

    Falls back to a synchronous run when the input needs chunking. A provider
    misconfiguration is a hard error for an explicit ``--async`` (the user asked
    for Batch specifically), but degrades to a synchronous run when async came
    from the persistent ``OM_OPENAI_ASYNC_MODE=batch`` env mode — so a scheduled
    reflect never hard-fails just because Batch isn't usable on this host.
    """
    import openai

    from .jobs import BatchProviderError, submit_reflect_batch
    from .reflect import ChunkingRequired
    from .usage.budgets import BudgetExceededError

    try:
        record = submit_reflect_batch(config)
    except ChunkingRequired as exc:
        click.echo(f"Input too large for a single Batch request ({exc}); running synchronously instead.", err=True)
        _run_reflector_sync_degrade(config)
        return
    except BudgetExceededError as exc:
        # A budget block is terminal: a synchronous fallback would hit the same cap.
        raise click.ClickException(str(exc)) from exc
    except openai.APIStatusError as exc:
        # Raw provider errors from the file upload + POST /batches (e.g. 400
        # billing_hard_limit_reached / BadRequestError, 429 insufficient_quota /
        # RateLimitError). Surface a clean, actionable message — no traceback.
        # The job record is only saved after a successful submit, so a failed
        # submit leaves no dangling job and writes nothing.
        raise click.ClickException(_openai_batch_submit_message(exc)) from exc
    except BatchProviderError as exc:
        if explicit:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Async Batch unavailable ({exc}); running synchronously instead.", err=True)
        _run_reflector_sync_degrade(config)
        return

    if record is None:
        click.echo("No observations to reflect on.")
        return
    click.echo(f"Submitted OpenAI Batch job {record.job_id} (batch {record.batch_id}).")
    click.echo("Apply the result later with: om jobs poll")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print pruned reflections without writing")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--drop-stale", is_flag=True, help="Drop stale snapshot entries instead of moving them")
@click.option("--namespace", default=None, help="Reserved for cluster namespace-scoped pruning")
@click.pass_context
def prune(ctx: click.Context, dry_run: bool, as_json: bool, drop_stale: bool, namespace: str | None) -> None:
    """Prune or mark stale reflection snapshot entries."""
    import json as json_mod

    from .reflect import _reindex_if_enabled
    from .reflection_metadata import ensure_reflection_metadata, prune_stale_snapshots
    from .startup_memory import refresh_startup_memory

    config = ctx.obj["config"]
    if not config.reflections_path.exists():
        raise click.ClickException("reflections.md does not exist.")
    action = "drop" if drop_stale else config.snapshot_expiry_action
    from datetime import datetime, timezone

    source_mtime = datetime.fromtimestamp(config.reflections_path.stat().st_mtime, tz=timezone.utc)
    text = ensure_reflection_metadata(config.reflections_path.read_text(), node="local", source_mtime=source_mtime)
    pruned, summary = prune_stale_snapshots(text, ttl_days=config.snapshot_ttl_days, action=action)
    if as_json:
        payload = {**summary.to_dict(), "dry_run": dry_run, "namespace": namespace}
        click.echo(json_mod.dumps(payload, indent=2, sort_keys=True))
    elif dry_run:
        click.echo(pruned, nl=not pruned.endswith("\n"))
    else:
        config.reflections_path.write_text(pruned)
        refresh_startup_memory(config)
        _reindex_if_enabled(config)
        click.echo(
            f"Pruned reflections: {summary.pruned} dropped, "
            f"{summary.stale_sectioned} moved, {summary.annotated} annotated"
        )


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
    type=click.Choice(["claude", "codex", "cowork", "claude-memory", "all"]),
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
    from .observe import observe_claude_transcript_backfill, observe_cowork_transcript_backfill
    from .reflect import run_reflector
    from .transcripts.claude import find_all_transcripts
    from .transcripts.codex import find_recent_sessions
    from .transcripts.cowork import find_all_transcripts as find_all_cowork

    config = ctx.obj["config"]

    # Discover transcripts
    all_transcripts: list[tuple[Path, str]] = []  # (path, source_label)

    if source in ("claude", "all"):
        for p in find_all_transcripts(config.claude_projects_dir):
            all_transcripts.append((p, "claude"))

    if source in ("codex", "all"):
        for p in find_recent_sessions(config.codex_home):
            all_transcripts.append((p, "codex"))

    if source in ("cowork", "all"):
        for p in find_all_cowork(config.cowork_sessions_dir):
            all_transcripts.append((p, "cowork"))

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
            elif src == "cowork":
                chars = observe_cowork_transcript_backfill(path, config, chunk_size)
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
            # Show first 5 lines of content. Use the payload's stripped content
            # (not r.document.content) so the human terminal output never leads
            # with a raw `<!--om: ...-->` / `<!--om-section: ...-->` comment —
            # the same content the --json path emits.
            lines = str(payload["content"]).strip().splitlines()
            for line in lines[:5]:
                click.echo(f"  {line}")
            if len(lines) > 5:
                click.echo(f"  ... ({len(lines) - 5} more lines)")
    else:
        click.echo("No results found.")


def _search_result_payload(result) -> dict[str, object]:
    """Normalize a search result for JSON and terminal rendering."""
    from .startup_memory import _strip_om_metadata

    metadata = dict(result.document.metadata)
    metadata.pop("source_start_line", None)
    qmd_line = metadata.get("qmd_line", metadata.get("line"))
    # Strip inline `<!--om: ...-->` metadata and `<!--om-section: ...-->`
    # provenance stamps from the displayed snippet so recall output never leads
    # with a raw HTML comment.
    content = _strip_om_metadata(result.document.content)[:500]
    return {
        "rank": result.rank,
        "score": result.score,
        "doc_id": result.document.doc_id,
        "source": result.document.source.value,
        "heading": result.document.heading,
        "content": content,
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
@click.option("--budget-chars", type=int, help="Maximum startup context characters to emit.")
@click.option("--cwd", "routing_cwd", help="Current working directory for task-aware startup routing.")
@click.option("--task", help="Current task summary for task-aware startup routing.")
@click.option("--for", "agent", help="Host agent name, e.g. codex, claude, cowork, hermes.")
@click.option(
    "--quality-report",
    is_flag=True,
    help="Print a startup-context quality report (duplicates, stale facts, budget by section) instead of the payload.",
)
@click.option("--json", "as_json", is_flag=True, help="With --quality-report, emit JSON.")
@click.pass_context
def context(
    ctx: click.Context,
    budget_chars: int | None,
    routing_cwd: str | None,
    task: str | None,
    agent: str | None,
    quality_report: bool,
    as_json: bool,
) -> None:
    """Generate session-start JSON with budgeted startup memory.

    Called by the SessionStart hook. Outputs JSON with additionalContext
    containing compact generated memory and recall handles. With
    ``--quality-report`` it prints a diagnostic instead.
    """
    import json as json_mod

    from .startup_memory import build_startup_payload, startup_quality_report

    config = ctx.obj["config"]

    if quality_report:
        report = startup_quality_report(config, budget_chars=budget_chars, cwd=routing_cwd, task=task, agent=agent)
        if as_json:
            click.echo(json_mod.dumps(report, indent=2))
        else:
            click.echo(_format_quality_report(report))
        return

    try:
        from .sync.config import cluster_feature_enabled, load_cluster_config
        from .sync.engine import sync_cluster

        cluster_config = load_cluster_config(config)
        if cluster_config and cluster_feature_enabled(config) and cluster_config.sync_before_context:
            sync_cluster(config, deadline_ms=cluster_config.startup_pull_deadline_ms, pull_only=True)
    except Exception:
        pass

    payload = build_startup_payload(config, budget_chars=budget_chars, cwd=routing_cwd, task=task, agent=agent)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": payload.text,
        }
    }
    click.echo(json_mod.dumps(output))


def _format_quality_report(report: dict) -> str:
    unique = report["duplicate_bullets"]
    lines = [
        "Startup context quality report",
        f"  budget: {report['used_chars']} / {report['budget_chars']} chars used",
        f"  duplicate bullets dropped: {report['duplicate_count']} occurrences ({len(unique)} unique)",
    ]
    for dup in unique:
        lines.append(f"    - {dup}")
    stale = report["stale_operational_facts"]
    lines.append(f"  stale operational facts: {len(stale)}")
    for fact in stale:
        age = f", {fact['age_days']}d" if fact.get("age_days") is not None else ""
        lines.append(f"    - [{fact['section']}] {fact['text']} (as of {fact['as_of']}{age})")
    lines.append("  budget by section:")
    for section in report["budget_by_section"]:
        lines.append(f"    {section['chars']:>6}  {section['heading']}")
    if report["overflow_handles"]:
        lines.append(f"  overflow (recall handles): {len(report['overflow_handles'])}")
        for handle in report["overflow_handles"]:
            lines.append(f"    - {handle}")
    growth = report.get("growth")
    if growth:
        from .growth import format_growth_lines

        lines.extend(f"  {line}" for line in format_growth_lines(growth))
    return "\n".join(lines)


@cli.command()
@click.option("--query", help="Search query for deeper memory recall.")
@click.option("--handle", help="Expansion handle from `om context`, e.g. startup:active:active-projects.")
@click.option("--limit", default=8, show_default=True, help="Maximum search results.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.option("--cwd", "routing_cwd", help="Current working directory to include in recall routing metadata.")
@click.option("--task", help="Current task summary to include in recall routing metadata.")
@click.option("--for", "agent", help="Host agent name, e.g. codex, claude, cowork, hermes.")
@click.pass_context
def recall(
    ctx: click.Context,
    query: str | None,
    handle: str | None,
    limit: int,
    as_json: bool,
    routing_cwd: str | None,
    task: str | None,
    agent: str | None,
) -> None:
    """Recall deeper memory by search query or startup expansion handle."""
    import json as json_mod
    import logging

    from .search import get_backend
    from .startup_memory import recall_handle
    from .talk import RecallStatus

    _LOGGER = logging.getLogger(__name__)

    if not query and not handle:
        raise click.UsageError("Provide --query or --handle.")

    config = ctx.obj["config"]
    output: dict[str, object] = {
        "query": query,
        "handle": handle,
        "cwd": routing_cwd,
        "task": task,
        "agent": agent,
        "results": [],
    }
    if handle:
        try:
            text = recall_handle(config, handle)
        except KeyError as e:
            raise click.ClickException(f"Unknown recall handle: {handle}") from e
        output["text"] = text
        if not query:
            # Keep the --json shape uniform: every recall payload carries
            # recall_status. A handle expansion is a direct file read, not a
            # backend search, so report it as a clean "ok".
            output["recall_status"] = RecallStatus.OK.value
            if as_json:
                click.echo(json_mod.dumps(output, indent=2, sort_keys=True))
            else:
                click.echo(text.rstrip())
            return

    # Run the same fail-closed search path the talk loop uses, so a ready backend
    # whose search() raises (degenerate corpus, QMD subprocess error) degrades to
    # recall_status="unavailable" instead of crashing this command with a traceback.
    backend = get_backend(config.search_backend, config)
    backend_ready = backend.is_ready()
    if backend_ready:
        try:
            results = backend.search(query or "", limit=limit)
            recall_status = RecallStatus.OK.value if results else RecallStatus.EMPTY.value
        except Exception as exc:
            _LOGGER.debug("recall search failed: %s", exc)
            results = []
            recall_status = RecallStatus.UNAVAILABLE.value
    else:
        results = []
        recall_status = RecallStatus.UNAVAILABLE.value
    payloads = [_search_result_payload(result) for result in results]
    output["results"] = payloads
    output["recall_status"] = recall_status
    if as_json:
        click.echo(json_mod.dumps(output, indent=2, sort_keys=True))
        return
    if handle and output.get("text"):
        click.echo(str(output["text"]).rstrip())
        click.echo()
        click.echo("---")
        click.echo()
    if not payloads:
        if recall_status == RecallStatus.UNAVAILABLE.value:
            click.echo(
                f"Recall backend '{config.search_backend}' is unavailable; no results. "
                f'Rebuild the index with `om search --reindex "<query>"`, then retry.'
            )
        else:
            click.echo("No recall results.")
        return
    for item in payloads:
        location = _format_location(item.get("source_path"), item.get("source_line"))  # type: ignore[arg-type]
        suffix = f" ({location})" if location else ""
        click.echo(f"[{item['rank']}] {item['heading'] or item['doc_id']}{suffix}")
        click.echo(str(item["content"]).rstrip())
        click.echo()


@cli.command()
@click.option("--query", help="Optional opening utterance to start the conversation.")
@click.option(
    "--backend",
    "backend_override",
    help="Search backend used for recall: moss, bm25, qmd, qmd-hybrid, or none. Defaults to OM_SEARCH_BACKEND.",
)
@click.option("--limit", default=5, show_default=True, help="Max memory snippets recalled per turn.")
@click.option("--max-turns", type=int, default=None, help="Stop after this many turns (default: until you exit).")
@click.option("--reindex", is_flag=True, help="Rebuild the search index before talking.")
@click.option("--for", "agent", help="Host agent name for profile/recall routing, e.g. claude, codex.")
@click.option("--json", "as_json", is_flag=True, help="Emit the full transcript as JSON when the conversation ends.")
@click.pass_context
def talk(
    ctx: click.Context,
    query: str | None,
    backend_override: str | None,
    limit: int,
    max_turns: int | None,
    reindex: bool,
    agent: str | None,
    as_json: bool,
) -> None:
    """Have a spoken-style conversation with your memories. (Experimental.)

    Each turn runs recall over your memories in the background and grounds the
    reply in what it finds. Type 'exit' (or Ctrl-D) to end. Text-only for now;
    pluggable voice providers (mic + speech) are planned on the same loop.
    Flags and output may change. See docs/talk-to-memories.md.
    """
    import json as json_mod

    from .search import reindex as reindex_index
    from .talk import Conversation, RecallEngine, TextTransport

    config = ctx.obj["config"]
    if backend_override:
        config.search_backend = backend_override

    if reindex:
        try:
            count = reindex_index(config)
            click.echo(f"Indexed {count} memory section(s) into '{config.search_backend}'.", err=True)
        except Exception as exc:  # never block the conversation on an index failure
            click.echo(f"om: reindex failed ({exc}); continuing with the existing index.", err=True)

    engine = RecallEngine(config)
    conversation = Conversation(
        config,
        engine,
        agent=agent,
        recall_limit=limit,
        recall_timeout=config.talk_recall_timeout,
    )
    # In --json mode stdout is reserved for the machine-readable transcript, so
    # the live conversation is rendered to stderr instead.
    transport = TextTransport(output_stream=sys.stderr if as_json else sys.stdout)

    # prepare() warms the recall backend (for Moss this downloads the index into
    # memory, which can take a few seconds), so flag it before the pause.
    click.echo(f"Preparing recall backend '{config.search_backend}'…", err=True)
    ready = conversation.prepare()
    status = "ready" if ready else "unavailable (replies will be ungrounded — try `om talk --reindex`)"
    click.echo(f"om talk — recall backend '{config.search_backend}' is {status}.", err=True)
    click.echo("Talking to your memories. Type 'exit' or press Ctrl-D to end.", err=True)

    turns: list[dict[str, object]] = []
    pending = (query or "").strip() or None
    try:
        while True:
            if max_turns is not None and len(turns) >= max_turns:
                break
            utterance = pending if pending is not None else transport.listen()
            pending = None
            if utterance is None:
                break
            turn = conversation.reply(utterance)
            transport.speak(turn.assistant)
            if turn.recalled:
                click.echo(f"  ⟢ grounded in {len(turn.recalled)} memory snippet(s)", err=True)
            elif turn.recall_status == "timeout":
                click.echo(
                    "  ⟢ memory search timed out this turn (set OM_TALK_RECALL_TIMEOUT higher "
                    "if your backend is slow to warm up)",
                    err=True,
                )
            elif turn.recall_status == "unavailable":
                click.echo("  ⟢ memory search unavailable — reply is ungrounded", err=True)
            turns.append(
                {
                    "user": turn.user,
                    "assistant": turn.assistant,
                    "grounded": turn.grounded,
                    "recall_status": turn.recall_status,
                    "error": turn.error,
                    "recalled": [
                        {
                            "doc_id": s.doc_id,
                            "heading": s.heading,
                            "source": s.source,
                            "score": s.score,
                        }
                        for s in turn.recalled
                    ],
                }
            )
    except KeyboardInterrupt:
        click.echo("", err=True)
    finally:
        conversation.close()
        transport.close()

    if as_json:
        click.echo(
            json_mod.dumps(
                {"backend": config.search_backend, "backend_ready": ready, "turns": turns},
                indent=2,
                sort_keys=True,
            )
        )


@cli.group()
def cluster() -> None:
    """Manage OM Cluster sync."""


@cluster.command("init")
@click.option("--name", default="Personal Memory", show_default=True, help="Cluster display name")
@click.option("--node-alias", default=None, help="Local node alias")
@click.option("--default-namespace", default="personal", show_default=True, help="Default memory namespace")
@click.option("--transport", multiple=True, help="Transport spec, e.g. filesystem:~/Sync/om-cluster")
@click.option("--import-existing/--no-import-existing", default=False, help="Import existing Markdown into records")
@click.option("--force", is_flag=True, help="Overwrite existing cluster config")
@click.pass_context
def cluster_init(
    ctx: click.Context,
    name: str,
    node_alias: str | None,
    default_namespace: str,
    transport: tuple[str, ...],
    import_existing: bool,
    force: bool,
) -> None:
    """Initialize a local OM Cluster."""
    from .sync.config import initialize_cluster_config
    from .sync.materialize import materialize_cluster_memory
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    transports = [_parse_transport_spec(spec) for spec in transport]
    try:
        cluster_config = initialize_cluster_config(
            config,
            name=name,
            node_alias=node_alias,
            default_namespace=default_namespace,
            transports=transports,
            force=force,
        )
    except FileExistsError as e:
        raise click.ClickException(str(e)) from e

    store = ClusterStore.from_config(config)
    store.ensure_layout()
    store.append_record(
        kind="node_membership",
        namespace=cluster_config.default_namespace,
        source={"agent": "cluster-init", "host_alias": cluster_config.node_alias},
        payload={
            "operation": "add",
            "node_id": cluster_config.node_id,
            "alias": cluster_config.node_alias,
            "signing_public_key": store.keypair.signing_public_key_b64,
            "encryption_public_key": store.keypair.encryption_public_key_b64,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    if import_existing:
        backup = _backup_existing_memory(config)
        _import_existing_memory(store)
        materialize_cluster_memory(config, store)
        if backup is not None:
            click.echo(f"Backed up existing Markdown to {backup}")
    click.echo(f"Initialized OM Cluster {cluster_config.name} ({cluster_config.id})")
    click.echo(f"Node: {cluster_config.node_alias} ({cluster_config.node_id})")


@cluster.command("invite")
@click.option("--expires", default="10m", show_default=True, help="Invite lifetime, e.g. 10m, 2h, 1d")
@click.option(
    "--mode",
    type=click.Choice(["request", "trusted-direct"]),
    default="request",
    show_default=True,
    help="Invite mode",
)
@click.pass_context
def cluster_invite(ctx: click.Context, expires: str, mode: str) -> None:
    """Create an invite token for another machine."""
    from .sync.config import create_invite_token, load_cluster_config

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    if mode == "trusted-direct":
        click.echo("Warning: this invite token carries cluster key material. Treat it like a private key.", err=True)
    else:
        click.echo("Created request-mode invite. The token does not carry cluster data keys.", err=True)
    click.echo(create_invite_token(config, cluster_config, expires=expires, mode=mode))


@cluster.command("join")
@click.argument("invite_token")
@click.option("--node-alias", default=None, help="Local node alias")
@click.option("--force", is_flag=True, help="Overwrite existing cluster config")
@click.pass_context
def cluster_join(ctx: click.Context, invite_token: str, node_alias: str | None, force: bool) -> None:
    """Join an OM Cluster using an invite token."""
    from .sync.config import join_cluster_from_invite
    from .sync.engine import build_transport
    from .sync.store import ClusterStore, NodeMetadata

    config = ctx.obj["config"]
    try:
        cluster_config, invite = join_cluster_from_invite(config, invite_token, node_alias=node_alias, force=force)
    except (FileExistsError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    issuer = invite["body"]
    join_request = invite.get("join_request")
    if join_request:
        for transport_config in cluster_config.transports:
            transport = build_transport(transport_config)
            transport.publish_join_request(
                cluster_config.id,
                join_request["request_id"],
                (json_like(join_request) + "\n").encode("utf-8"),
            )
        click.echo(f"Created pending OM Cluster join request {join_request['request_id']}")
        click.echo(f"Cluster: {cluster_config.name} ({cluster_config.id})")
        click.echo(f"Node: {cluster_config.node_alias} ({cluster_config.node_id})")
        click.echo("Run `om cluster requests` on a trusted node, then approve this request.")
        return

    store = ClusterStore.from_config(config)
    store.ensure_layout()
    store.write_node_metadata(
        NodeMetadata(
            node_id=issuer["issuer_node_id"],
            alias=issuer["issuer_alias"],
            signing_public_key_b64=issuer["issuer_signing_public_key_b64"],
        )
    )
    store.append_record(
        kind="node_membership",
        namespace=cluster_config.default_namespace,
        source={"agent": "cluster-join", "host_alias": cluster_config.node_alias},
        payload={
            "operation": "add",
            "node_id": cluster_config.node_id,
            "alias": cluster_config.node_alias,
            "signing_public_key": store.keypair.signing_public_key_b64,
            "encryption_public_key": store.keypair.encryption_public_key_b64,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "invite": invite,
        },
    )
    click.echo(f"Joined OM Cluster {cluster_config.name} ({cluster_config.id})")
    click.echo(f"Node: {cluster_config.node_alias} ({cluster_config.node_id})")


@cluster.command("status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.pass_context
def cluster_status(ctx: click.Context, as_json: bool) -> None:
    """Show cluster status."""
    import json as json_mod

    from .sync.config import cluster_feature_enabled, load_cluster_config, load_pending_join_state
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        data = {"initialized": False, "enabled": False}
    else:
        pending_state = load_pending_join_state(config, cluster_config.id)
        try:
            store = ClusterStore.from_config(config)
        except FileNotFoundError:
            data = {
                "initialized": True,
                "enabled": False,
                "cluster": {"id": cluster_config.id, "name": cluster_config.name},
                "node": {"id": cluster_config.node_id, "alias": cluster_config.node_alias},
                "join_request": {
                    "status": (pending_state or {}).get("status", "pending"),
                    "request_id": (pending_state or {}).get("request_id"),
                    "reason": (pending_state or {}).get("reason"),
                },
                "transports": [transport.to_dict() for transport in cluster_config.transports],
            }
            if as_json:
                click.echo(json_mod.dumps(data, indent=2, sort_keys=True))
                return
            click.echo(f"Cluster: {data['cluster']['name']} ({data['cluster']['id']})")
            click.echo(f"Node: {data['node']['alias']} ({data['node']['id']})")
            click.echo(f"Join request: {data['join_request']['status']} ({data['join_request']['request_id']})")
            if data["join_request"].get("reason"):
                click.echo(f"Reason: {data['join_request']['reason']}")
            return
        records = store.list_records(include_tombstoned=True)
        pending_peers = _visible_pending_peers(store)
        data = {
            "initialized": True,
            "enabled": cluster_feature_enabled(config),
            "cluster": {"id": cluster_config.id, "name": cluster_config.name},
            "node": {"id": cluster_config.node_id, "alias": cluster_config.node_alias},
            "transports": [transport.to_dict() for transport in cluster_config.transports],
            "transport_diagnostics": _transport_diagnostics(cluster_config),
            "heads": store.all_heads(),
            "peers": {node_id: node.to_dict() for node_id, node in store.public_nodes().items()},
            "pending_peers": pending_peers,
            "review_artifacts": _cluster_review_artifacts(store),
            "records": {
                "total": len(records),
                "observations": len([r for r in records if r.kind == "observation"]),
                "reflection_snapshots": len([r for r in records if r.kind == "reflection_snapshot"]),
                "manual_overrides": len([r for r in records if r.kind == "manual_override"]),
                "tombstones": len([r for r in records if r.kind == "tombstone"]),
            },
            "materialized": {
                "observations": config.observations_path.exists(),
                "reflections": config.reflections_path.exists(),
                "profile": config.profile_path.exists(),
                "active": config.active_path.exists(),
            },
        }
        if pending_state:
            data["join_request"] = {
                "status": pending_state.get("status"),
                "request_id": pending_state.get("request_id"),
                "reason": pending_state.get("reason"),
            }
        data["remediation"] = _cluster_remediation(data)
    if as_json:
        click.echo(json_mod.dumps(data, indent=2, sort_keys=True))
        return
    if not data["initialized"]:
        click.echo("OM Cluster: not initialized")
        return
    click.echo(f"Cluster: {data['cluster']['name']} ({data['cluster']['id']})")
    click.echo(f"Node: {data['node']['alias']} ({data['node']['id']})")
    click.echo(f"Enabled: {str(data['enabled']).lower()}")
    click.echo(f"Local records: {data['records']['total']}")
    click.echo("Heads:")
    for node_id, seq in data["heads"].items():
        alias = data["peers"].get(node_id, {}).get("alias", node_id)
        click.echo(f"  {node_id} {alias} seq={seq}")
    if data.get("pending_peers"):
        click.echo("Pending peers:")
        for node_id, peer in data["pending_peers"].items():
            click.echo(f"  {node_id} {peer.get('alias', node_id)}")
    if data.get("remediation"):
        click.echo("Remediation:")
        for item in data["remediation"]:
            click.echo(f"  - {item}")
    if data.get("join_request"):
        click.echo(f"Join request: {data['join_request']['status']} ({data['join_request']['request_id']})")


def _visible_pending_peers(store) -> dict[str, dict]:
    approved = set(store.public_nodes())
    return {node_id: node.to_dict() for node_id, node in store.pending_nodes().items() if node_id not in approved}


def _cluster_review_artifacts(store) -> dict[str, object]:
    conflict_path = store.cluster_dir / "review" / "reflection-conflicts.json"
    if not conflict_path.exists():
        return {"reflection_conflicts": {"count": 0, "path": None}}
    try:
        data = json.loads(conflict_path.read_text())
        count = int(data.get("count", 0))
    except Exception:
        count = -1
    return {"reflection_conflicts": {"count": count, "path": str(conflict_path)}}


def _transport_diagnostics(cluster_config) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    for transport in cluster_config.transports:
        item: dict[str, object] = {"type": transport.type, "path": transport.path, "reachable": None}
        try:
            if transport.type == "filesystem" and transport.path:
                path = Path(transport.path).expanduser()
                item.update(
                    {"reachable": path.exists(), "writable": os.access(path, os.W_OK) if path.exists() else False}
                )
            elif transport.type == "relay" and transport.path:
                from .sync.transports.relay import RelayTransport

                health = RelayTransport(transport.path, timeout_seconds=2.0).health()
                item.update({"reachable": bool(health.get("ok")), "health": health})
            elif transport.type == "p2p" and transport.path:
                item.update({"reachable": None, "message": "P2P reachability is checked during sync."})
        except Exception as e:
            item.update({"reachable": False, "last_error": str(e)})
        diagnostics.append(item)
    return diagnostics


def _cluster_remediation(data: dict) -> list[str]:
    items: list[str] = []
    if data.get("pending_peers"):
        items.append(
            "Inspect pending public node metadata with `om cluster status --json`; "
            "approve only request-mode joins you recognize."
        )
    join_request = data.get("join_request")
    if isinstance(join_request, dict) and join_request.get("status") == "pending":
        items.append(
            "This node is waiting for approval; ask an existing member to run "
            "`om cluster requests` and `om cluster approve`."
        )
    for transport in data.get("transport_diagnostics", []):
        if isinstance(transport, dict) and transport.get("reachable") is False:
            items.append(
                f"Transport {transport.get('type')} is unreachable; verify relay service, shared directory, "
                "or network access."
            )
    review = data.get("review_artifacts")
    if isinstance(review, dict):
        conflicts = review.get("reflection_conflicts")
        if isinstance(conflicts, dict) and conflicts.get("count", 0):
            items.append(f"Review reflection conflicts at {conflicts.get('path')}.")
    return items


@cluster.group("relay")
def cluster_relay() -> None:
    """Run and inspect an OM Cluster relay."""


@cluster_relay.command("serve")
@click.option("--storage-dir", required=True, type=click.Path(path_type=Path), help="Relay artifact directory.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
def cluster_relay_serve(storage_dir: Path, host: str, port: int) -> None:
    """Run the supported file-backed relay server."""
    from .sync.relay_server import serve_relay

    server = serve_relay(storage_dir, host=host, port=port)
    bind_host, bind_port = server.server_address
    click.echo(f"OM Cluster relay serving http://{bind_host}:{bind_port} storage={storage_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@cluster_relay.command("health")
@click.argument("url", required=False)
@click.option("--artifact-dir", type=click.Path(path_type=Path), help="Local relay artifact directory to scan.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def cluster_relay_health(ctx: click.Context, url: str | None, artifact_dir: Path | None, as_json: bool) -> None:
    """Check relay reachability and artifact secrecy."""
    import json as json_mod

    from .sync.config import load_cluster_config
    from .sync.relay_server import scan_relay_artifacts
    from .sync.transports.relay import RelayTransport

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    urls = [url] if url else []
    if not urls and cluster_config:
        urls = [
            transport.path for transport in cluster_config.transports if transport.type == "relay" and transport.path
        ]
    checks = []
    for relay_url in urls:
        try:
            checks.append({"url": relay_url, **RelayTransport(relay_url).health()})
        except Exception as e:
            checks.append({"url": relay_url, "ok": False, "error": str(e)})
    artifact_scan = scan_relay_artifacts(artifact_dir) if artifact_dir else None
    payload = {"checks": checks, "artifact_scan": artifact_scan, "ok": all(item.get("ok") for item in checks)}
    if artifact_scan is not None:
        payload["ok"] = bool(payload["ok"] and artifact_scan["ok"])
    if as_json:
        click.echo(json_mod.dumps(payload, indent=2, sort_keys=True))
        return
    if not checks and artifact_scan is None:
        click.echo("No relay URL or artifact directory configured.")
        return
    for item in checks:
        status = "ok" if item.get("ok") else "failed"
        click.echo(f"{item['url']}: {status}")
        if item.get("error"):
            click.echo(f"  error: {item['error']}")
    if artifact_scan is not None:
        status = "ok" if artifact_scan["ok"] else "failed"
        click.echo(f"artifact scan: {status} ({artifact_scan['file_count']} files)")


@cluster.command("requests")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.pass_context
def cluster_requests(ctx: click.Context, as_json: bool) -> None:
    """List pending request-mode join requests visible in configured transports."""
    import json as json_mod

    from .sync.config import load_cluster_config, verify_join_request
    from .sync.engine import build_transport

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    requests: dict[str, dict] = {}
    for transport_config in cluster_config.transports:
        transport = build_transport(transport_config)
        for request_id in transport.list_join_requests(cluster_config.id):
            data = transport.fetch_join_request(cluster_config.id, request_id)
            if data is None:
                continue
            try:
                request = verify_join_request(json_mod.loads(data.decode("utf-8")), cluster_id=cluster_config.id)
            except Exception as e:
                requests[request_id] = {"request_id": request_id, "status": "invalid", "error": str(e)}
                continue
            node = request["node"]
            requests[request_id] = {
                "request_id": request_id,
                "status": "pending",
                "node_id": node["node_id"],
                "alias": node.get("alias", node["node_id"]),
                "invite_id": request.get("invite_id"),
                "requested_at": request.get("requested_at"),
                "expires_at": request.get("expires_at"),
            }
    if as_json:
        click.echo(json_mod.dumps(list(requests.values()), indent=2, sort_keys=True))
        return
    if not requests:
        click.echo("No join requests.")
        return
    for request in requests.values():
        if request["status"] == "invalid":
            click.echo(f"{request['request_id']} invalid: {request['error']}")
        else:
            click.echo(
                f"{request['request_id']} pending {request['alias']} ({request['node_id']}) "
                f"expires={request['expires_at']}"
            )


@cluster.command("approve")
@click.argument("request_id")
@click.pass_context
def cluster_approve(ctx: click.Context, request_id: str) -> None:
    """Approve a request-mode join request."""
    _complete_join_request(ctx, request_id, approve=True, reason="")


@cluster.command("reject")
@click.argument("request_id")
@click.option("--reason", default="manual-reject", show_default=True, help="Rejection reason")
@click.pass_context
def cluster_reject(ctx: click.Context, request_id: str, reason: str) -> None:
    """Reject a request-mode join request."""
    _complete_join_request(ctx, request_id, approve=False, reason=reason)


def _complete_join_request(ctx: click.Context, request_id: str, *, approve: bool, reason: str) -> None:
    import json as json_mod

    from .sync.config import (
        create_join_approval,
        create_join_rejection,
        load_cluster_config,
        verify_join_request,
    )
    from .sync.engine import build_transport, sync_cluster
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    store = ClusterStore.from_config(config)
    request = None
    transports = [build_transport(transport_config) for transport_config in cluster_config.transports]
    for transport in transports:
        data = transport.fetch_join_request(cluster_config.id, request_id)
        if data is None:
            continue
        try:
            request = verify_join_request(json_mod.loads(data.decode("utf-8")), cluster_id=cluster_config.id)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        break
    if request is None:
        raise click.ClickException(f"No join request found for {request_id}")

    if approve:
        node = request["node"]
        record = store.append_record(
            kind="node_membership",
            namespace=store.cluster_config.default_namespace,
            source={"agent": "cluster-approve", "host_alias": store.cluster_config.node_alias},
            payload={
                "operation": "add",
                "node_id": node["node_id"],
                "alias": node.get("alias", node["node_id"]),
                "signing_public_key": node["signing_public_key_b64"],
                "encryption_public_key": node.get("encryption_public_key_b64"),
                "approved_by_node_id": store.cluster_config.node_id,
                "request_id": request_id,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        approval = create_join_approval(
            config,
            cluster_config,
            request=request,
            membership_record_id=record.record_id,
            approved_by_node_id=store.cluster_config.node_id,
        )
        sync_cluster(config, materialize=False)
    else:
        record = None
        approval = create_join_rejection(config, cluster_config, request=request, reason=reason)

    payload = (json_mod.dumps(approval, indent=2, sort_keys=True) + "\n").encode("utf-8")
    for transport in transports:
        transport.publish_join_approval(cluster_config.id, request_id, payload)
    if approve and record is not None:
        click.echo(f"Approved {request_id} with membership record {record.record_id}")
    else:
        click.echo(f"Rejected {request_id}")


@cluster.command("peers")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.pass_context
def cluster_peers(ctx: click.Context, as_json: bool) -> None:
    """List trusted cluster peers."""
    import json as json_mod

    from .sync.store import ClusterStore

    store = ClusterStore.from_config(ctx.obj["config"])
    peers = [node.to_dict() for node in store.public_nodes().values()]
    if as_json:
        click.echo(json_mod.dumps(peers, indent=2, sort_keys=True))
        return
    for peer in peers:
        revoked = " revoked" if peer.get("revoked") else ""
        click.echo(f"{peer['node_id']} {peer['alias']}{revoked}")


@cluster.group("namespace")
def cluster_namespace() -> None:
    """Manage cluster namespaces."""


@cluster_namespace.command("list")
@click.pass_context
def cluster_namespace_list(ctx: click.Context) -> None:
    from .sync.config import load_cluster_config

    cluster_config = load_cluster_config(ctx.obj["config"])
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    namespaces = {cluster_config.default_namespace}
    namespaces.update(rule.namespace for rule in cluster_config.namespace_rules)
    for namespace in sorted(namespaces):
        default = " (default)" if namespace == cluster_config.default_namespace else ""
        click.echo(f"{namespace}{default}")


@cluster_namespace.command("add")
@click.argument("namespace")
@click.pass_context
def cluster_namespace_add(ctx: click.Context, namespace: str) -> None:
    from .sync.config import NamespaceRule, load_cluster_config, write_cluster_config

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    if namespace in {cluster_config.default_namespace, *[rule.namespace for rule in cluster_config.namespace_rules]}:
        click.echo(f"Namespace already exists: {namespace}")
        return
    updated = replace(
        cluster_config,
        namespace_rules=[*cluster_config.namespace_rules, NamespaceRule(namespace=namespace)],
    )
    write_cluster_config(config, updated)
    click.echo(f"Added namespace {namespace}")


@cluster_namespace.command("remove")
@click.argument("namespace")
@click.pass_context
def cluster_namespace_remove(ctx: click.Context, namespace: str) -> None:
    from .sync.config import load_cluster_config, write_cluster_config

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    if namespace == cluster_config.default_namespace:
        raise click.ClickException("Cannot remove the default namespace.")
    rules = [rule for rule in cluster_config.namespace_rules if rule.namespace != namespace]
    write_cluster_config(config, replace(cluster_config, namespace_rules=rules))
    click.echo(f"Removed namespace {namespace}")


@cluster.group("source-policy")
def cluster_source_policy() -> None:
    """Manage source-to-namespace policies."""


@cluster_source_policy.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.pass_context
def cluster_source_policy_list(ctx: click.Context, as_json: bool) -> None:
    import json as json_mod

    from .sync.config import load_cluster_config

    cluster_config = load_cluster_config(ctx.obj["config"])
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    rules = [rule.__dict__ for rule in cluster_config.namespace_rules]
    if as_json:
        click.echo(json_mod.dumps(rules, indent=2, sort_keys=True))
        return
    for index, rule in enumerate(rules, 1):
        filters = ", ".join(f"{key}={value}" for key, value in rule.items() if key != "namespace" and value)
        click.echo(f"{index}. {rule['namespace']} {filters}".rstrip())


@cluster_source_policy.command("add")
@click.option("--agent", default=None, help="Agent/source name to match")
@click.option("--git-remote", "git_remote_hash", default=None, help="Hashed git remote/project ID to match")
@click.option("--path-contains", default=None, help="Local path substring for future local-only routing")
@click.option("--namespace", required=True, help="Destination namespace")
@click.option("--local-only", is_flag=True, help="Mark this rule as local-only")
@click.pass_context
def cluster_source_policy_add(
    ctx: click.Context,
    agent: str | None,
    git_remote_hash: str | None,
    path_contains: str | None,
    namespace: str,
    local_only: bool,
) -> None:
    from .sync.config import NamespaceRule, load_cluster_config, write_cluster_config

    config = ctx.obj["config"]
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    rule = NamespaceRule(
        source=agent,
        path_contains=path_contains,
        git_remote_hash=git_remote_hash,
        namespace=namespace,
        local_only=local_only,
    )
    write_cluster_config(config, replace(cluster_config, namespace_rules=[*cluster_config.namespace_rules, rule]))
    click.echo(f"Added source policy for namespace {namespace}")


@cluster.command("sync")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--no-materialize", is_flag=True, help="Do not rebuild Markdown after pull")
@click.pass_context
def cluster_sync(ctx: click.Context, as_json: bool, no_materialize: bool) -> None:
    """Sync records through configured transports."""
    import json as json_mod

    from .sync.engine import sync_cluster

    summary = sync_cluster(ctx.obj["config"], materialize=not no_materialize)
    if as_json:
        click.echo(json_mod.dumps(summary.to_dict(), indent=2, sort_keys=True))
        return
    click.echo(f"Pulled {summary.pulled} record(s)")
    click.echo(f"Pushed {summary.pushed} record(s)")
    if summary.rejected:
        click.echo(f"Rejected {summary.rejected} record(s)")
    if summary.materialized:
        click.echo("Materialized observations.md, reflections.md, profile.md, active.md")


@cluster.command("materialize")
@click.option("--no-reindex", is_flag=True, help="Skip search reindex")
@click.pass_context
def cluster_materialize(ctx: click.Context, no_reindex: bool) -> None:
    """Rebuild local Markdown views from records."""
    from .sync.materialize import materialize_cluster_memory
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    summary = materialize_cluster_memory(config, ClusterStore.from_config(config), reindex=not no_reindex)
    click.echo("Materialized cluster memory" if summary.any_written else "Materialized files already current")


@cluster.command("provenance")
@click.argument("query_or_record_id")
@click.pass_context
def cluster_provenance(ctx: click.Context, query_or_record_id: str) -> None:
    """Inspect record provenance by ID or plaintext query over local records."""
    from .sync.store import ClusterStore

    store = ClusterStore.from_config(ctx.obj["config"])
    matches = []
    for record in store.list_records(include_tombstoned=True):
        payload = store.read_payload(record)
        if query_or_record_id == record.record_id or query_or_record_id.lower() in json_like(payload).lower():
            matches.append((record, payload))
    if not matches:
        click.echo("No matching records.")
        return
    for record, payload in matches:
        source = record.data.get("source", {})
        click.echo(f"Record: {record.record_id}")
        click.echo(f"Kind: {record.kind}")
        click.echo(f"Namespace: {record.namespace}")
        click.echo(f"Node: {source.get('host_alias', record.node_id)} ({record.node_id})")
        click.echo(f"Agent: {source.get('agent', 'unknown')}")
        if source.get("project"):
            click.echo(f"Project: {source['project']}")
        if source.get("transcript_id"):
            click.echo(f"Transcript: {source['transcript_id']}")
        click.echo(f"Payload hash: {record.payload_hash}")
        if len(matches) > 1:
            click.echo("")


@cluster.command("redact")
@click.option("--record", "record_id", required=True, help="Record ID to tombstone")
@click.option("--reason", default="user-redaction", show_default=True, help="Redaction reason")
@click.pass_context
def cluster_redact(ctx: click.Context, record_id: str, reason: str) -> None:
    """Create a tombstone for a record."""
    from .sync.materialize import materialize_cluster_memory
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    store = ClusterStore.from_config(config)
    tombstone = store.append_record(
        kind="tombstone",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "manual", "host_alias": store.cluster_config.node_alias},
        payload={
            "target_record_id": record_id,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    materialize_cluster_memory(config, store)
    click.echo(f"Created tombstone {tombstone.record_id}")


@cluster.command("revoke")
@click.argument("node_id")
@click.option("--reason", default="manual-revoke", show_default=True, help="Revocation reason")
@click.pass_context
def cluster_revoke(ctx: click.Context, node_id: str, reason: str) -> None:
    """Revoke a peer for future records."""
    from .sync.store import ClusterStore

    store = ClusterStore.from_config(ctx.obj["config"])
    record = store.append_record(
        kind="node_membership",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "manual", "host_alias": store.cluster_config.node_alias},
        payload={
            "operation": "revoke",
            "node_id": node_id,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    click.echo(f"Revoked {node_id} with record {record.record_id}")


@cluster.command("rotate-key")
@click.pass_context
def cluster_rotate_key(ctx: click.Context) -> None:
    """Rotate the cluster data key for future records."""
    import secrets

    from .sync.crypto import wrap_key_for_node
    from .sync.store import ClusterStore, new_data_key_b64

    store = ClusterStore.from_config(ctx.obj["config"])
    key_id = f"key_{store.cluster_config.node_id}_{secrets.token_hex(8)}"
    data_key_b64 = new_data_key_b64()
    recipients = []
    excluded = []
    for node in store.public_nodes().values():
        if node.revoked:
            excluded.append(node.node_id)
            continue
        if not node.encryption_public_key_b64:
            excluded.append(node.node_id)
            continue
        recipients.append(
            {
                "node_id": node.node_id,
                "wrapped_key": wrap_key_for_node(
                    data_key_b64,
                    node.encryption_public_key_b64,
                    aad=f"{store.cluster_config.id}:{key_id}".encode("utf-8"),
                ),
            }
        )
    if not any(recipient["node_id"] == store.cluster_config.node_id for recipient in recipients):
        raise click.ClickException("Local node has no encryption public key for key epoch rotation.")
    record = store.append_record(
        kind="key_epoch",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "manual", "host_alias": store.cluster_config.node_alias},
        payload={
            "epoch": len(store.secret.data_keys) + 1,
            "key_id": key_id,
            "recipients": recipients,
            "excluded_nodes": excluded,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    click.echo(f"Rotated cluster data key to {key_id} with record {record.record_id}")


@cluster.command("reencrypt")
@click.option("--from-key", "from_key_id", default=None, help="Only rewrap records encrypted with this key ID.")
@click.option("--limit", type=int, default=None, help="Maximum records to rewrap in this run.")
@click.option("--dry-run", is_flag=True, help="Report records that would be rewrapped without writing records.")
@click.pass_context
def cluster_reencrypt(ctx: click.Context, from_key_id: str | None, limit: int | None, dry_run: bool) -> None:
    """Append rewrap records for historical payloads under the active key."""
    from .sync.ids import validate_key_id
    from .sync.store import ClusterStore

    if from_key_id is not None:
        validate_key_id(from_key_id)
    if limit is not None and limit < 1:
        raise click.ClickException("--limit must be greater than zero.")
    store = ClusterStore.from_config(ctx.obj["config"])
    candidates = store.rewrap_candidates(from_key_id=from_key_id)
    if limit is not None:
        candidates = candidates[:limit]
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "active_key_id": store.secret.active_key_id,
                    "candidate_count": len(candidates),
                    "records": [
                        {
                            "record_id": record.record_id,
                            "kind": record.kind,
                            "namespace": record.namespace,
                            "node_id": record.node_id,
                            "key_id": record.data.get("encryption", {}).get("key_id"),
                        }
                        for record in candidates
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    written = [store.append_payload_rewrap(record) for record in candidates]
    click.echo(f"Rewrapped {len(written)} historical payload(s) under {store.secret.active_key_id}")


@cluster.command("purge-old-ciphertext")
@click.option("--key-id", required=True, help="Old key ID to inspect for purge readiness.")
@click.option("--yes", is_flag=True, help="Acknowledge destructive purge intent.")
@click.pass_context
def cluster_purge_old_ciphertext(ctx: click.Context, key_id: str, yes: bool) -> None:
    """Report old-ciphertext records that have active-key rewrap coverage."""
    from .sync.ids import validate_key_id
    from .sync.store import ClusterStore

    validate_key_id(key_id)
    store = ClusterStore.from_config(ctx.obj["config"])
    candidates = store.rewrap_candidates(from_key_id=key_id)
    report = {
        "key_id": key_id,
        "active_key_id": store.secret.active_key_id,
        "unrewrapped_count": len(candidates),
        "warning": (
            "Purging old ciphertext is destructive and must include shared transports and backups. "
            "This command reports readiness only; it does not delete record files."
        ),
    }
    click.echo(json.dumps(report, indent=2, sort_keys=True))
    if yes:
        raise click.ClickException(
            "Automatic old-ciphertext deletion is not implemented; inspect transports/backups manually."
        )


@cluster.group("p2p")
def cluster_p2p() -> None:
    """Inspect optional direct peer transport configuration."""


@cluster_p2p.command("peers")
@click.pass_context
def cluster_p2p_peers(ctx: click.Context) -> None:
    """List configured direct peer endpoints."""
    from .sync.config import load_cluster_config

    cluster_config = load_cluster_config(ctx.obj["config"])
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    peers = []
    for transport in cluster_config.transports:
        if transport.type == "p2p" and transport.path:
            peers.extend([peer for peer in transport.path.split(",") if peer])
    click.echo(json.dumps({"peers": peers}, indent=2, sort_keys=True))


@cluster_p2p.command("status")
@click.pass_context
def cluster_p2p_status(ctx: click.Context) -> None:
    """Show direct peer transport status without authorizing peers."""
    from .sync.config import load_cluster_config

    cluster_config = load_cluster_config(ctx.obj["config"])
    if cluster_config is None:
        raise click.ClickException("OM Cluster is not initialized.")
    peer_count = sum(
        len([peer for peer in transport.path.split(",") if peer])
        for transport in cluster_config.transports
        if transport.type == "p2p" and transport.path
    )
    click.echo(
        json.dumps(
            {
                "configured": peer_count > 0,
                "peer_count": peer_count,
                "trust_note": "Direct peer reachability does not grant OM Cluster membership.",
            },
            indent=2,
            sort_keys=True,
        )
    )


@cluster.group("override")
def cluster_override() -> None:
    """Manage profile/active manual override records."""


@cluster_override.command("add")
@click.option("--target", type=click.Choice(["profile", "active"]), required=True)
@click.option("--section", required=True)
@click.option("--body", required=True)
@click.pass_context
def cluster_override_add(ctx: click.Context, target: str, section: str, body: str) -> None:
    ctx.invoke(cluster_override_set, target=target, section=section, body=body)


@cluster_override.command("set")
@click.option("--target", type=click.Choice(["profile", "active"]), required=True)
@click.option("--section", required=True)
@click.option("--body", required=True)
@click.pass_context
def cluster_override_set(ctx: click.Context, target: str, section: str, body: str) -> None:
    from .sync.materialize import materialize_cluster_memory
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    store = ClusterStore.from_config(config)
    record = store.append_record(
        kind="manual_override",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "manual", "host_alias": store.cluster_config.node_alias},
        payload={"target": target, "section": section, "operation": "upsert", "body": body},
    )
    materialize_cluster_memory(config, store)
    click.echo(f"Set override {target}:{section} with record {record.record_id}")


@cluster_override.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.pass_context
def cluster_override_list(ctx: click.Context, as_json: bool) -> None:
    import json as json_mod

    from .sync.store import ClusterStore

    store = ClusterStore.from_config(ctx.obj["config"])
    rows = []
    for record in store.list_records(kind="manual_override"):
        payload = store.read_payload(record)
        rows.append(
            {
                "record_id": record.record_id,
                "target": payload.get("target"),
                "section": payload.get("section"),
                "operation": payload.get("operation", "upsert"),
                "namespace": record.namespace,
                "hlc": record.hlc,
            }
        )
    if as_json:
        click.echo(json_mod.dumps(rows, indent=2, sort_keys=True))
        return
    for row in rows:
        click.echo(f"{row['record_id']} {row['operation']} {row['target']}:{row['section']}")


@cluster_override.command("get")
@click.option("--target", type=click.Choice(["profile", "active"]), required=True)
@click.option("--section", required=True)
@click.pass_context
def cluster_override_get(ctx: click.Context, target: str, section: str) -> None:
    from .sync.store import ClusterStore

    store = ClusterStore.from_config(ctx.obj["config"])
    matches = []
    for record in store.list_records(kind="manual_override"):
        payload = store.read_payload(record)
        if payload.get("target") == target and payload.get("section") == section:
            matches.append((record, payload))
    if not matches:
        raise click.ClickException(f"No override found for {target}:{section}")
    record, payload = max(matches, key=lambda row: (row[0].hlc, row[0].record_id))
    if payload.get("operation") == "remove":
        raise click.ClickException(f"Override removed for {target}:{section}")
    click.echo(str(payload.get("body") or ""))


@cluster_override.command("remove")
@click.argument("override_record_id", required=False)
@click.option("--target", type=click.Choice(["profile", "active"]), default=None)
@click.option("--section", default=None)
@click.pass_context
def cluster_override_remove(
    ctx: click.Context,
    override_record_id: str | None,
    target: str | None,
    section: str | None,
) -> None:
    if override_record_id:
        ctx.invoke(cluster_redact, record_id=override_record_id, reason="override-removed")
        return
    if not target or not section:
        raise click.ClickException("Pass an override_record_id or --target and --section.")
    from .sync.materialize import materialize_cluster_memory
    from .sync.store import ClusterStore

    config = ctx.obj["config"]
    store = ClusterStore.from_config(config)
    record = store.append_record(
        kind="manual_override",
        namespace=store.cluster_config.default_namespace,
        source={"agent": "manual", "host_alias": store.cluster_config.node_alias},
        payload={"target": target, "section": section, "operation": "remove"},
    )
    materialize_cluster_memory(config, store)
    click.echo(f"Removed override {target}:{section} with record {record.record_id}")


def _parse_transport_spec(spec: str):
    from .sync.config import TransportConfig

    kind, sep, value = spec.partition(":")
    if not sep or kind not in {"filesystem", "relay", "p2p"} or not value:
        raise click.ClickException("Use filesystem:PATH, relay:URL, or p2p:URL[,URL...].")
    if kind == "filesystem":
        return TransportConfig(type="filesystem", path=_expand_transport_path(value))
    urls = [url.strip() for url in value.split(",") if url.strip()]
    if not urls or any(not url.startswith(("http://", "https://")) for url in urls):
        raise click.ClickException(f"{kind} transports must use HTTP(S) peer URLs.")
    return TransportConfig(type=kind, path=",".join(urls))


def _expand_transport_path(value: str) -> str:
    if sys.platform == "win32":
        import re

        value = re.sub(r"%([^%]+)%", lambda match: os.environ.get(match.group(1), match.group(0)), value)
    return os.path.expandvars(os.path.expanduser(value))


@cli.group()
def mail() -> None:
    """OM Mail: email inboxes as a portable memory substrate (experimental)."""


def _mail_exceptions() -> tuple[type[Exception], ...]:
    from .mail.account import MailAccountError
    from .mail.envelope import EnvelopeError
    from .mail.pack import PackError
    from .mail.provider import MailProviderError
    from .mail.service import MailServiceError

    return (MailAccountError, EnvelopeError, PackError, MailProviderError, MailServiceError)


@mail.command("init")
@click.option(
    "--provider",
    "provider_name",
    default=None,
    help="Mail provider: agentmail or localdir (default: OM_MAIL_PROVIDER).",
)
@click.option("--username", default=None, help="Requested inbox username (provider-generated if omitted).")
@click.option("--display-name", default=None, help="Inbox display name.")
@click.option("--force", is_flag=True, help="Replace an existing mail account.")
@click.pass_context
def mail_init(
    ctx: click.Context,
    provider_name: str | None,
    username: str | None,
    display_name: str | None,
    force: bool,
) -> None:
    """Mint a dynamic inbox and a signing identity for this host."""
    from .mail import build_mail_provider
    from .mail.account import MailAccount, load_mail_account, new_mail_keypair, write_mail_account

    config = ctx.obj["config"]
    existing = load_mail_account(config)
    if existing is not None and not force:
        raise click.ClickException(f"Mail account already configured ({existing.address}). Use --force to replace it.")
    try:
        provider = build_mail_provider(config, provider_name)
        inbox = provider.create_inbox(username=username, display_name=display_name)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    private_b64, public_b64 = new_mail_keypair()
    account = MailAccount(
        provider=provider.name,
        inbox_id=inbox.inbox_id,
        address=inbox.address,
        display_name=inbox.display_name or display_name,
        signing_private_key_b64=private_b64,
        signing_public_key_b64=public_b64,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    write_mail_account(config, account)
    click.echo(f"Mail account ready: {inbox.address} (provider: {provider.name})")
    click.echo(f"Signing public key: {public_b64}")
    click.echo("Share the address and public key with peers; they pin them with `om mail peers add`.")
    click.echo("For context packs, exchange a shared key out of band: `om mail peers new-shared-key`.")


@mail.command("status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_status(ctx: click.Context, as_json: bool) -> None:
    """Show the mail account, pinned peers, held mail, and sync cursor."""
    from .mail.account import list_held, load_mail_account, load_mail_peers, load_mail_state

    config = ctx.obj["config"]
    account = load_mail_account(config)
    if account is None:
        raise click.ClickException("No mail account configured. Run `om mail init` first.")
    peers = load_mail_peers(config)
    held = list_held(config)
    state = load_mail_state(config)
    payload = {
        "address": account.address,
        "provider": account.provider,
        "inbox_id": account.inbox_id,
        "signing_public_key_b64": account.signing_public_key_b64,
        "peers": sorted(peers),
        "held": len(held),
        "cursor": state.cursor,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"Address: {account.address} (provider: {account.provider})")
    click.echo(f"Signing public key: {account.signing_public_key_b64}")
    click.echo(f"Peers: {len(peers)}  Held messages: {len(held)}  Cursor: {state.cursor or '-'}")


@mail.group("peers")
def mail_peers() -> None:
    """Manage pinned peers (the local trust anchor for inbound mail)."""


@mail_peers.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_peers_list(ctx: click.Context, as_json: bool) -> None:
    """List pinned peers and their permissions."""
    from .mail.account import load_mail_peers

    peers = load_mail_peers(ctx.obj["config"])
    payload = [
        {
            "address": peer.address,
            "alias": peer.alias,
            "signing_public_key_b64": peer.signing_public_key_b64,
            "has_shared_key": bool(peer.shared_key_b64),
            "allow_recall": peer.allow_recall,
            "auto_accept": peer.auto_accept,
        }
        for peer in sorted(load_mail_peers(ctx.obj["config"]).values(), key=lambda p: p.address)
    ]
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not peers:
        click.echo("No peers pinned. Add one with `om mail peers add ADDRESS --key PUBKEY`.")
        return
    for item in payload:
        flags = [
            "shared-key" if item["has_shared_key"] else None,
            "allow-recall" if item["allow_recall"] else None,
            "auto-accept" if item["auto_accept"] else None,
        ]
        alias = f" ({item['alias']})" if item["alias"] else ""
        suffix = ", ".join(flag for flag in flags if flag) or "signed-only"
        click.echo(f"{item['address']}{alias}: {suffix}")


@mail_peers.command("add")
@click.argument("address")
@click.option("--alias", default=None, help="Friendly name for this peer.")
@click.option("--key", "signing_public_key_b64", required=True, help="Peer's Ed25519 signing public key (b64url).")
@click.option("--shared-key", "shared_key_b64", default=None, help="Out-of-band symmetric key for encrypted payloads.")
@click.option(
    "--allow-recall", is_flag=True, help="Answer this peer's recall requests during `om mail sync --respond`."
)
@click.option("--auto-accept", is_flag=True, help="Ingest this peer's memory notes without explicit `om mail accept`.")
@click.pass_context
def mail_peers_add(
    ctx: click.Context,
    address: str,
    alias: str | None,
    signing_public_key_b64: str,
    shared_key_b64: str | None,
    allow_recall: bool,
    auto_accept: bool,
) -> None:
    """Pin a peer's address and signing key (re-running re-pins)."""
    from .mail.account import MailPeer, upsert_peer

    peer = MailPeer(
        address=address.strip().lower(),
        alias=alias,
        signing_public_key_b64=signing_public_key_b64,
        shared_key_b64=shared_key_b64,
        allow_recall=allow_recall,
        auto_accept=auto_accept,
    )
    upsert_peer(ctx.obj["config"], peer)
    click.echo(f"Pinned peer {peer.address}.")


@mail_peers.command("remove")
@click.argument("address")
@click.pass_context
def mail_peers_remove(ctx: click.Context, address: str) -> None:
    """Remove a pinned peer."""
    from .mail.account import remove_peer

    if not remove_peer(ctx.obj["config"], address):
        raise click.ClickException(f"No pinned peer: {address}")
    click.echo(f"Removed peer {address.strip().lower()}.")


@mail_peers.command("new-shared-key")
def mail_peers_new_shared_key() -> None:
    """Mint a symmetric pack key; exchange it out of band, never over email."""
    from .mail.account import new_shared_key_b64

    click.echo(new_shared_key_b64())
    click.echo("Share this key out of band (password manager, in person) — never over email.", err=True)
    click.echo("Both sides store it with `om mail peers add ADDRESS --key PUBKEY --shared-key KEY`.", err=True)


@mail.command("send-note")
@click.argument("address")
@click.option("--text", default=None, help="Note markdown.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--subject", default=None, help="Short note subject.")
@click.pass_context
def mail_send_note(
    ctx: click.Context,
    address: str,
    text: str | None,
    file_path: Path | None,
    subject: str | None,
) -> None:
    """Mail a memory note to a pinned peer (scope-filtered before sending)."""
    from .mail.service import send_note

    if bool(text) == bool(file_path):
        raise click.UsageError("Provide exactly one of --text or --file.")
    markdown = text if text is not None else file_path.read_text()  # type: ignore[union-attr]
    try:
        result = send_note(ctx.obj["config"], to=address, markdown=markdown, subject=subject)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    encrypted = "encrypted" if result.get("encrypted") else "signed-plaintext"
    click.echo(f"Sent memory-note {result['envelope_id']} to {result['to']} ({encrypted}).")


@mail.command("send-pack")
@click.argument("address")
@click.option(
    "--include",
    default="profile.md,active.md,reflections.md",
    show_default=True,
    help="Comma-separated memory files to pack.",
)
@click.pass_context
def mail_send_pack(ctx: click.Context, address: str, include: str) -> None:
    """Mail an encrypted, scope-filtered context pack to a pinned peer."""
    from .mail.service import send_pack

    files = tuple(item.strip() for item in include.split(",") if item.strip())
    try:
        result = send_pack(ctx.obj["config"], to=address, include=files)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Sent context-pack {result['envelope_id']} to {result['to']}: {', '.join(result['files'])}")


@mail.command("ask")
@click.argument("address")
@click.option("--query", required=True, help="Recall query for the peer's memory.")
@click.option("--limit", default=8, show_default=True, help="Maximum results requested.")
@click.option(
    "--wait", "wait_seconds", default=0.0, show_default=True, help="Seconds to poll for the answer (0 = don't wait)."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_ask(
    ctx: click.Context,
    address: str,
    query: str,
    limit: int,
    wait_seconds: float,
    as_json: bool,
) -> None:
    """Ask a peer's memory a question (recall negotiation over email)."""
    from .mail.service import ask

    try:
        result = ask(ctx.obj["config"], to=address, query=query, limit=limit, wait_seconds=wait_seconds)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    if result.get("status") == "answered":
        response = result.get("response", {})
        results = response.get("results", [])
        if not results:
            click.echo(f"Peer answered: no relevant memory ({response.get('recall_status', 'empty')}).")
            return
        for item in results:
            click.echo(f"[{item.get('rank')}] {item.get('heading') or ''}".rstrip())
            click.echo(str(item.get("content", "")).rstrip())
            click.echo()
        return
    if result.get("status") == "timeout":
        click.echo(
            f"No answer within {wait_seconds:g}s. The peer answers on their next `om mail sync --respond`; "
            f"pick it up later with `om mail sync` (request {result['request_id']})."
        )
        return
    click.echo(
        f"Request {result['request_id']} sent. The peer answers on their next `om mail sync --respond`; "
        "pick it up later with `om mail sync`."
    )


@mail.command("sync")
@click.option("--respond", is_flag=True, help="Answer pending recall requests from peers marked allow-recall.")
@click.option("--limit", default=50, show_default=True, help="Maximum messages fetched per sync.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_sync_cmd(ctx: click.Context, respond: bool, limit: int, as_json: bool) -> None:
    """Fetch, verify, and route inbound mail (poll-based; nothing auto-executes)."""
    from .mail.service import mail_sync

    try:
        report = mail_sync(ctx.obj["config"], respond=respond, limit=limit)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
        return
    click.echo(
        "Mail sync: "
        f"{report.get('fetched', 0)} fetched, {report.get('ingested', 0)} ingested, "
        f"{report.get('responded', 0)} responded, {report.get('packs', 0)} packs, "
        f"{report.get('responses', 0)} responses, {report.get('held', 0)} held, "
        f"{report.get('skipped', 0)} skipped."
    )
    for detail in report.get("details", []):
        click.echo(f"  - {detail}")
    if report.get("held"):
        click.echo("Held mail needs review: `om mail inbox`, then `om mail accept|reject MESSAGE_ID`.")


@mail.command("inbox")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_inbox(ctx: click.Context, as_json: bool) -> None:
    """List held messages awaiting review (quarantine, fail-closed)."""
    from .mail.account import list_held

    held = list_held(ctx.obj["config"])
    if as_json:
        click.echo(json.dumps(held, indent=2, sort_keys=True))
        return
    if not held:
        click.echo("No held messages.")
        return
    for record in held:
        sender = record.get("sender", "?")
        subject = record.get("subject", "")
        click.echo(f"{record.get('message_id', '?')}: from {sender} — {record.get('reason', '?')} ({subject})")


@mail.command("accept")
@click.argument("message_id")
@click.pass_context
def mail_accept(ctx: click.Context, message_id: str) -> None:
    """Ingest a held memory note into observations (re-verified, fail-closed)."""
    from .mail.service import accept_held

    try:
        result = accept_held(ctx.obj["config"], message_id)
    except _mail_exceptions() as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Ingested memory-note from {result.get('sender', '?')} into observations.")


@mail.command("reject")
@click.argument("message_id")
@click.pass_context
def mail_reject(ctx: click.Context, message_id: str) -> None:
    """Discard a held message without ingesting it."""
    from .mail.service import reject_held

    if not reject_held(ctx.obj["config"], message_id):
        raise click.ClickException(f"No held message: {message_id}")
    click.echo(f"Rejected {message_id}.")


@mail.command("search")
@click.option("--query", required=True, help="Substring to find in the local mail corpus.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def mail_search(ctx: click.Context, query: str, as_json: bool) -> None:
    """Search the local mail corpus (held mail, recall responses, opened packs).

    Accepted notes flow into observations and are covered by `om search` /
    `om recall`; this command covers the mail-side artifacts that have not
    (or will never) become durable memory.
    """
    from .mail.account import mail_dir

    base = mail_dir(ctx.obj["config"])
    needle = query.lower()
    matches: list[dict[str, str]] = []
    for subdir in ("held", "responses", "packs"):
        root = base / subdir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append({"path": str(path), "line": str(line_number), "text": line.strip()[:200]})
    if as_json:
        click.echo(json.dumps(matches, indent=2, sort_keys=True))
        return
    if not matches:
        click.echo("No matches in the local mail corpus.")
        return
    for match in matches:
        click.echo(f"{match['path']}:{match['line']}: {match['text']}")


def _backup_existing_memory(config: Config) -> Path | None:
    """Snapshot existing Markdown before a cluster init.

    Thin shim over the generalized backup module so cluster-init backups share
    the same on-disk format and rotation. Returns the snapshot dir (or ``None``
    when there is nothing to back up).

    Forces the snapshot regardless of ``OM_BACKUP_ENABLED``: cluster init is a
    one-shot destructive migration (it overwrites reflections/profile/active via
    materialize), so this safety net must not be defeatable by the global backup
    toggle.
    """
    from .backup import create_snapshot

    info = create_snapshot(config, reason="cluster-init", force=True)
    return info.path if info is not None else None


def _import_existing_memory(store) -> None:
    config = store.config
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if config.observations_path.exists() and config.observations_path.read_text().strip():
        store.append_record(
            kind="observation",
            namespace=store.cluster_config.default_namespace,
            source={"agent": "legacy-local-import", "host_alias": store.cluster_config.node_alias},
            payload={
                "format": "markdown",
                "body": config.observations_path.read_text(),
                "observed_at": now,
                "message_count": 0,
                "retention": "recent",
            },
        )
    if config.reflections_path.exists() and config.reflections_path.read_text().strip():
        # LEAK-CRITICAL: this is a share-OUT path (it writes a shared cluster
        # record), so the legacy reflections import must route through the same
        # default-deny allowlist as every other share-out path (the cluster
        # reflector and the Moss upload). Importing the raw file would sync
        # scope=local and any explicit-unknown scope off-host as plaintext.
        from .reflection_metadata import filter_reflection_entries_for_cluster

        store.append_record(
            kind="reflection_snapshot",
            namespace=store.cluster_config.default_namespace,
            source={"agent": "legacy-local-import", "host_alias": store.cluster_config.node_alias},
            payload={
                "format": "markdown",
                "body": filter_reflection_entries_for_cluster(config.reflections_path.read_text()),
                "frontier": store.records_frontier(),
                "input_record_ids": [record.record_id for record in store.list_records(kind="observation")],
                "base_snapshot_ids": [],
            },
        )


def json_like(value) -> str:
    import json as json_mod

    return json_mod.dumps(value, sort_keys=True, ensure_ascii=False)


@cli.command(name="export")
@click.option(
    "--target",
    type=click.Choice(["generic", "chatgpt", "claude-managed-agents"]),
    default="generic",
    show_default=True,
    help="Memory platform bundle to generate.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Output directory. Defaults to a timestamped directory under the OM memory dir.",
)
@click.option(
    "--include-observations",
    is_flag=True,
    help="Include recent raw observations. Off by default because they are more transient.",
)
@click.option("--overwrite", is_flag=True, help="Replace a non-empty output directory.")
@click.pass_context
def export_cmd(
    ctx: click.Context,
    target: str,
    output: Path | None,
    include_observations: bool,
    overwrite: bool,
) -> None:
    """Export local OM memory as a platform-ready seed bundle."""
    from .platform_export import export_platform_memory

    config = ctx.obj["config"]

    try:
        result = export_platform_memory(
            config,
            target=target,
            output_dir=output,
            include_observations=include_observations,
            overwrite=overwrite,
        )
    except (FileExistsError, ValueError) as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Exported {result.target} memory bundle to {result.output_dir}")
    for exported in result.files:
        click.echo(f"  {exported.path.relative_to(result.output_dir)}")


@cli.command(hidden=True, name="codex-checkpoint")
@click.pass_context
def codex_checkpoint(ctx: click.Context) -> None:
    """Queue a Codex transcript-specific checkpoint from the Stop hook payload."""
    import json as json_mod

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
        worker_pid = _spawn_detached(worker_command, cwd=payload.get("cwd"))
        if worker_pid is not None:
            _write_checkpoint_lock_owner(lock_path, pid=worker_pid)
    except Exception:
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="failed",
        )
        _release_codex_checkpoint_lock(lock_path)
        raise


def _run_codex_checkpoint_observer(config: Config, transcript: Path) -> None:
    from .observe import observe_codex_transcript

    observe_codex_transcript(transcript, config, dry_run=False)
    _maybe_run_reflector_catchup(config)


@cli.command(hidden=True, name="codex-checkpoint-worker")
@click.option("--transcript", type=click.Path(path_type=Path), required=True)
@click.pass_context
def codex_checkpoint_worker(ctx: click.Context, transcript: Path) -> None:
    """Observe one Codex transcript and release its checkpoint lock."""
    config = ctx.obj["config"]
    transcript = transcript.expanduser()
    lock_path = _codex_checkpoint_lock_path(config, transcript)
    _write_checkpoint_lock_owner(lock_path)

    try:
        _run_bounded_observer_call(config, _run_codex_checkpoint_observer, config, transcript)
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="success",
        )
    except ObserverWorkerBusy:
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="skipped_busy",
        )
        return
    except ObserverWorkerTimeout:
        _update_codex_checkpoint_state(
            config,
            transcript,
            message_count=_count_codex_transcript_messages(transcript),
            status="timeout",
        )
        return
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


def _build_claude_checkpoint_worker_command(transcript: Path) -> list[str]:
    """Return argv for the detached Claude checkpoint worker process."""
    om_path = _find_om_path() or sys.argv[0] or "om"
    return [om_path, "claude-checkpoint-worker", "--transcript", str(transcript)]


def _spawn_detached(argv: list[str], cwd: str | Path | None = None) -> int | None:
    """Spawn *argv* as a detached background process.

    Uses ``start_new_session`` on POSIX and ``CREATE_NEW_PROCESS_GROUP |
    DETACHED_PROCESS`` on Windows so the worker survives the parent hook
    process exiting.
    """
    import subprocess

    popen_kwargs: dict[str, object] = {
        "cwd": cwd or None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)
    return getattr(proc, "pid", None)


@cli.command(hidden=True, name="claude-checkpoint")
@click.pass_context
def claude_checkpoint(ctx: click.Context) -> None:
    """Queue a Claude Code session checkpoint from a hook payload.

    Mirrors the POSIX ``session-end.sh`` hook: parses the JSON payload from
    stdin, throttles in-session checkpoints by
    ``OM_SESSION_OBSERVER_INTERVAL_SECONDS`` (skipping force events
    SessionEnd/Stop), holds a per-transcript filesystem lock, persists
    state to ``.session-observer-state.json``, and spawns a detached
    ``claude-checkpoint-worker`` so the calling agent isn't blocked by
    LLM work.
    """
    import json as json_mod

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

    event_name = payload.get("hook_event_name") or ""
    is_force_event = event_name in {"SessionEnd", "Stop", ""}
    is_checkpoint_event = event_name in {"UserPromptSubmit", "PreCompact"}

    # Cheap early-exit for disabled checkpoints (no lock needed).
    if is_checkpoint_event and not is_force_event:
        disable = (os.environ.get("OM_DISABLE_SESSION_OBSERVER_CHECKPOINTS") or "").strip().lower()
        if disable in {"1", "true", "yes", "on"}:
            return

    lock_path = _checkpoint_lock_path(config.claude_checkpoint_lock_dir, transcript)
    if not _acquire_checkpoint_lock(config.claude_checkpoint_lock_dir, lock_path):
        return

    try:
        current_count = _count_claude_transcript_messages(transcript)

        # Throttle non-force events: skip if no new messages since the last
        # observation OR if the last observation was within the configured
        # interval. Force events (SessionEnd/Stop) always proceed.
        if not is_force_event:
            state = _load_checkpoint_state(config.claude_checkpoint_state_path)
            previous = state.get(str(transcript), {})
            previous_count = previous.get("message_count")
            previous_observed = previous.get("last_observed")
            interval = _session_observer_interval_seconds()

            if isinstance(previous_count, int) and previous_count >= current_count:
                _release_checkpoint_lock(lock_path)
                return

            if (
                interval > 0
                and isinstance(previous_observed, int)
                and (datetime.now(timezone.utc).timestamp() - previous_observed) < interval
            ):
                _release_checkpoint_lock(lock_path)
                return

        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=current_count,
            status="in_progress",
        )

        worker_command = _build_claude_checkpoint_worker_command(transcript)
        worker_pid = _spawn_detached(worker_command, cwd=payload.get("cwd"))
        if worker_pid is not None:
            _write_checkpoint_lock_owner(lock_path, pid=worker_pid)
    except Exception:
        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=_count_claude_transcript_messages(transcript),
            status="failed",
        )
        _release_checkpoint_lock(lock_path)
        # Hooks must never raise into the parent agent.
        return


def _run_claude_checkpoint_observer(config: Config, transcript: Path) -> None:
    from .observe import observe_claude_transcript

    observe_claude_transcript(transcript, config, dry_run=False)
    _maybe_run_reflector_catchup(config)


@cli.command(hidden=True, name="claude-checkpoint-worker")
@click.option("--transcript", type=click.Path(path_type=Path), required=True)
@click.pass_context
def claude_checkpoint_worker(ctx: click.Context, transcript: Path) -> None:
    """Observe one Claude transcript and release its checkpoint lock."""
    config = ctx.obj["config"]
    transcript = transcript.expanduser()
    lock_path = _checkpoint_lock_path(config.claude_checkpoint_lock_dir, transcript)
    _write_checkpoint_lock_owner(lock_path)

    try:
        _run_bounded_observer_call(config, _run_claude_checkpoint_observer, config, transcript)
        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=_count_claude_transcript_messages(transcript),
            status="success",
        )
    except ObserverWorkerBusy:
        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=_count_claude_transcript_messages(transcript),
            status="skipped_busy",
        )
        return
    except ObserverWorkerTimeout:
        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=_count_claude_transcript_messages(transcript),
            status="timeout",
        )
        return
    except Exception:
        _update_checkpoint_state(
            config.claude_checkpoint_state_path,
            transcript,
            message_count=_count_claude_transcript_messages(transcript),
            status="failed",
        )
        # Suppress to keep the worker from exiting non-zero into a
        # background context where nothing reads the status.
        return
    finally:
        _release_checkpoint_lock(lock_path)


_SUPPORTED_PROVIDERS = (
    "anthropic",
    "openai",
    "anthropic-vertex",
    "anthropic-bedrock",
    "openai-chatgpt",
    "xai-oauth",
    "xai",
)
_SCHEDULER_MODES = ("auto", "launchd", "cron", "schtasks", "none")
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

    if selected in {"openai-chatgpt", "xai-oauth"}:
        from .config import _has_subscription_tokens

        if not _has_subscription_tokens(selected):
            click.echo(
                f"Provider '{selected}' uses your subscription. "
                f"Run `om login {selected}` to sign in (skipping for now)."
            )
    elif selected == "xai":
        env_var = "XAI_API_KEY"
        existing = os.environ.get(env_var)
        if not existing:
            if non_interactive:
                raise click.ClickException(
                    f"Provider '{selected}' requires {env_var}. Set it in env "
                    f"or {config.env_file} before --non-interactive install."
                )
            key = click.prompt("Paste your xAI API key", hide_input=True).strip()
            if key:
                updates[env_var] = key
                os.environ[env_var] = key
    elif selected in {"anthropic", "openai"}:
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
@click.option("--cowork", "targets", flag_value="cowork", help="Install Cowork plugin")
@click.option("--grok", "targets", flag_value="grok", help="Install Grok Build TUI hooks (Claude compat aware)")
@click.option("--opencode", "targets", flag_value="opencode", help="Install OpenCode plugin and AGENTS fallback")
@click.option("--kimi", "targets", flag_value="kimi", help="Install Kimi Code CLI hooks")
@click.option("--both", "targets", flag_value="both", default=True, help="Install Claude Code + Codex (default)")
@click.option(
    "--all",
    "targets",
    flag_value="all",
    help="Install all integrations including OpenCode, Kimi, Cowork, and Grok",
)
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
    """Set up observational memory for Claude Code, Codex, OpenCode, Kimi, Grok, and/or Cowork."""
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

    if targets in ("claude", "both", "all"):
        _install_claude_hooks(config)

    if targets in ("codex", "both", "all"):
        _install_codex(config)

    if targets in ("cowork", "all"):
        _install_cowork_plugin(config)

    if targets in ("opencode", "all"):
        _install_opencode(config)

    if targets in ("grok", "all"):
        _install_grok(config)

    if targets in ("kimi", "all"):
        _install_kimi(config)

    if scheduler_mode == "launchd":
        try:
            _install_launchd(config, targets)
        except click.ClickException as exc:
            click.echo(f"Warning: launchd scheduler setup failed: {exc}", err=True)
        else:
            _uninstall_cron(targets)
    elif scheduler_mode == "schtasks":
        try:
            _install_schtasks(config, targets)
        except click.ClickException as exc:
            click.echo(f"Warning: schtasks scheduler setup failed: {exc}", err=True)
    elif scheduler_mode == "cron":
        _install_cron(config, targets)
        if sys.platform == "darwin":
            _uninstall_launchd(config, targets)
    elif scheduler_mode == "none":
        if sys.platform == "darwin":
            _uninstall_launchd(config, targets)
        if sys.platform == "win32":
            _uninstall_schtasks(config, targets)
        else:
            _uninstall_cron(targets)

    click.echo("\nInstallation complete! Run 'om status' to verify.")


@cli.command()
@click.option("--claude", "targets", flag_value="claude")
@click.option("--codex", "targets", flag_value="codex")
@click.option("--cowork", "targets", flag_value="cowork")
@click.option("--grok", "targets", flag_value="grok", help="Remove Grok Build TUI hooks")
@click.option("--opencode", "targets", flag_value="opencode", help="Remove OpenCode plugin and AGENTS fallback")
@click.option("--kimi", "targets", flag_value="kimi", help="Remove Kimi Code CLI hooks")
@click.option("--both", "targets", flag_value="both", default=True)
@click.option("--all", "targets", flag_value="all")
@click.option("--purge", is_flag=True, help="Also remove memory files")
@click.pass_context
def uninstall(ctx: click.Context, targets: str, purge: bool) -> None:
    """Remove OM hooks/scheduler jobs for selected targets (claude/codex/opencode/kimi/grok/cowork)."""
    config = ctx.obj["config"]

    if targets in ("claude", "both", "all"):
        _uninstall_claude_hooks(config)

    if targets in ("codex", "both", "all"):
        _uninstall_codex(config)

    if targets in ("cowork", "all"):
        _uninstall_cowork_plugin(config)

    if targets in ("opencode", "all"):
        _uninstall_opencode(config)

    if targets in ("grok", "all"):
        _uninstall_grok(config)

    if targets in ("kimi", "all"):
        _uninstall_kimi(config)

    _uninstall_launchd(config, targets)
    _uninstall_schtasks(config, targets)
    if sys.platform != "win32":
        # crontab calls only make sense on POSIX hosts.
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

    # LLM provider/model status (shared summary with `om auth status`)
    from .auth import provider_summary_lines

    click.echo("\nLLM:")
    for line in provider_summary_lines(config):
        click.echo(line)
    click.echo(f"  Anthropic API key: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    click.echo(f"  OpenAI API key: {'set' if os.environ.get('OPENAI_API_KEY') else 'not set'}")
    click.echo(f"  xAI API key: {'set' if os.environ.get('XAI_API_KEY') else 'not set'}")
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

    if sys.platform == "win32":
        schtasks_jobs = _schtasks_job_statuses(config)
        installed_tasks = [job for job in schtasks_jobs if job["installed"]]
        missing_tasks = [str(job["key"]) for job in schtasks_jobs if not job["installed"]]
        task_errors = [f"{job['key']}: {job['error']}" for job in schtasks_jobs if job["error"]]
        click.echo(f"  Scheduled tasks: {len(installed_tasks)}/{len(schtasks_jobs)} installed")
        if missing_tasks:
            click.echo(f"  Missing: {', '.join(missing_tasks)}")
        if task_errors:
            click.echo(f"  schtasks: {', '.join(task_errors)}")
    else:
        cron_jobs, cron_error = _om_cron_jobs()
        if cron_error:
            click.echo(f"  Cron jobs: error ({cron_error})")
        elif cron_jobs:
            click.echo(f"  Cron jobs: {len(cron_jobs)} found ({', '.join(sorted(cron_jobs))})")
            if sys.platform == "darwin" and any(job["installed"] for job in launchd_jobs):
                click.echo("  Duplicate backstops: launchd and cron are both present")
        else:
            click.echo("  Cron jobs: none")

    # Cowork plugin
    cowork_plugin_dir = _cowork_plugin_dir(config)
    if cowork_plugin_dir.exists():
        click.echo("\nCowork plugin: installed")
        click.echo(f"  Path: {cowork_plugin_dir}")
        hooks_json = cowork_plugin_dir / "hooks" / "hooks.json"
        if hooks_json.exists():
            valid, detail = _validate_cowork_hooks_json(hooks_json)
            click.echo(f"  hooks.json: {'valid' if valid else f'invalid ({detail})'}")
        else:
            click.echo("  hooks.json: not found")
    else:
        click.echo("\nCowork plugin: not installed")
        click.echo(f"  Expected at: {_cowork_plugin_dir(config)}")

    # Cowork sessions
    from .transcripts.cowork import find_all_transcripts as find_all_cowork

    cowork_transcripts = find_all_cowork(config.cowork_sessions_dir)
    click.echo(f"  Sessions: {len(cowork_transcripts)} audit.jsonl file(s) found")

    # OpenCode status
    opencode_plugin = config.opencode_plugins_dir / _OPENCODE_PLUGIN_NAME
    click.echo("\nOpenCode:")
    click.echo(f"  Plugins dir: {config.opencode_plugins_dir}")
    opencode_plugin_status = "installed" if opencode_plugin.exists() else "not installed (run `om install --opencode`)"
    opencode_agents_status = "not installed"
    if config.opencode_agents_md.exists() and _OPENCODE_OM_MARKER in config.opencode_agents_md.read_text():
        opencode_agents_status = "installed"
    opencode_event_count = 0
    if config.opencode_events_dir.exists():
        opencode_event_count = len(list(config.opencode_events_dir.glob("*.jsonl")))
    click.echo(f"  OM plugin: {opencode_plugin_status}")
    click.echo(f"  AGENTS fallback: {opencode_agents_status}")
    click.echo(f"  Event logs: {opencode_event_count} file(s)")

    # Kimi Code CLI status
    click.echo("\nKimi Code CLI:")
    click.echo(f"  Config: {config.kimi_config_path}")
    if config.kimi_config_path.exists():
        try:
            kimi_config = config.kimi_config_path.read_text()
            if _KIMI_OM_BLOCK_START in kimi_config and _KIMI_OM_BLOCK_END in kimi_config:
                click.echo("  OM hooks: installed")
            else:
                click.echo("  OM hooks: not installed (run `om install --kimi`)")
        except Exception:
            click.echo("  OM hooks: config present (unreadable)")
    else:
        click.echo("  OM hooks: not installed (run `om install --kimi`)")
    if config.kimi_om_events_path.exists():
        from .transcripts.kimi import count_events

        click.echo(f"  Event log: {count_events(config.kimi_om_events_path)} event(s) at {config.kimi_om_events_path}")
    else:
        click.echo(f"  Event log: none yet ({config.kimi_om_events_path})")

    # Grok Build TUI status (Phase 1 hook support)
    grok_hook_file = config.grok_hooks_dir / "observational-memory.json"
    click.echo("\nGrok Build TUI:")
    click.echo(f"  Hooks dir: {config.grok_hooks_dir}")
    if grok_hook_file.exists():
        click.echo(f"  OM hook file: installed ({grok_hook_file})")
        try:
            import json as json_mod

            data = json_mod.loads(grok_hook_file.read_text())
            events = list(data.get("hooks", {}).keys())
            click.echo(f"  Registered events: {', '.join(events) if events else 'none'}")
        except Exception:
            click.echo("  OM hook file: present (unreadable)")
    else:
        click.echo("  OM hook file: not installed (run `om install --grok`)")

    if _has_om_claude_session_start(config):
        click.echo("  Claude compatibility: OM hooks in ~/.claude/settings.json (Grok will inherit context)")

    grok_config = config.grok_config_path
    if grok_config.exists():
        try:
            import tomllib

            with grok_config.open("rb") as f:
                gdata = tomllib.load(f)
            mem = gdata.get("memory", {})
            enabled = mem.get("enabled", False)
            status = "enabled" if enabled else "disabled"
            click.echo(f"  Native memory: {status} in {grok_config} (OM is independent peer)")
        except Exception:
            click.echo(f"  Native memory: config present at {grok_config} (parse error)")
    else:
        click.echo(f"  Native memory: no {grok_config} (disabled by default)")

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

    # 6b. Subscription auth store
    try:
        from .auth import auth_file_path
        from .auth.cli_import import detect_cli_imports
        from .auth.openai_chatgpt import access_token_is_expiring as _chatgpt_expiring
        from .auth.store import load_auth_store
        from .auth.xai_oauth import access_token_is_expiring as _xai_expiring

        store_path = auth_file_path(config)
        store = load_auth_store(config)
        providers = list((store.get("providers") or {}).keys())
        if providers:
            if not sys.platform == "win32" and store_path.exists():
                mode = store_path.stat().st_mode & 0o777
                if mode == 0o600:
                    _check("Auth store permissions", "PASS", f"{store_path} (0600)")
                else:
                    _check(
                        "Auth store permissions",
                        "WARN",
                        f"{oct(mode)} (too open)",
                        fix=f"chmod 600 {store_path}",
                    )
            _check("Subscription tokens", "PASS", ", ".join(providers))
            for pid in providers:
                state = (store.get("providers") or {}).get(pid) or {}
                access = (state.get("tokens") or {}).get("access_token") or ""
                if pid == "openai-chatgpt" and _chatgpt_expiring(access, 24 * 60 * 60):
                    _check(
                        f"{pid} expiry",
                        "WARN",
                        "access token expires within 24h (refresh on next use)",
                    )
                elif pid == "xai-oauth" and _xai_expiring(access, 24 * 60 * 60):
                    _check(
                        f"{pid} expiry",
                        "WARN",
                        "access token expires within 24h (refresh on next use)",
                    )
        else:
            detected = detect_cli_imports()
            if detected:
                _check(
                    "Subscription tokens",
                    "WARN",
                    f"none in {store_path}; sibling CLIs detected: {', '.join(detected)}",
                    fix="Run: om login --import",
                )
            else:
                _check("Subscription tokens", "PASS", "none configured (using API keys)")
    except Exception as exc:
        _check("Subscription tokens", "WARN", f"could not inspect auth store: {exc}")

    # 7. Env file permissions
    if config.env_file.exists():
        if sys.platform == "win32":
            # POSIX modes don't map to NTFS ACLs; %APPDATA% is per-user by
            # default, so we skip the chmod check on Windows.
            _check("Env file permissions", "PASS", "per-user APPDATA (Windows ACL)")
        else:
            mode = config.env_file.stat().st_mode & 0o777
            if mode == 0o600:
                _check("Env file permissions", "PASS", "600 (owner-only)")
            else:
                _check(
                    "Env file permissions",
                    "WARN",
                    f"{oct(mode)} (too open)",
                    fix=f"Run: chmod 600 {config.env_file}",
                )
    else:
        _check("Env file permissions", "WARN", "env file not found", fix="Run: om install")

    # 8. jq installed (only required by the bash-based hook scripts on POSIX)
    if sys.platform == "win32":
        _check("jq installed", "PASS", "skipped (Windows hooks invoke om directly)")
    elif shutil.which("jq"):
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

    # 9b. Moss search backend health (cloud-backed; opt-in). Never prints the key.
    if config.search_backend == "moss":
        try:
            import moss as _moss  # noqa: F401

            sdk_ok = True
        except Exception:
            sdk_ok = False
        if not sdk_ok:
            _check(
                "Moss SDK", "FAIL", "moss package not installed", fix='Run: pip install "observational-memory[voice]"'
            )
        else:
            _check("Moss SDK", "PASS", "installed")
        if config.moss_credentials() is not None:
            _check(
                "Moss credentials",
                "PASS",
                f"project configured; recall uploads memory to service.usemoss.dev (index '{config.moss_index_name}')",
            )
        else:
            _check(
                "Moss credentials",
                "FAIL",
                "OM_MOSS_PROJECT_ID / OM_MOSS_PROJECT_KEY not set",
                fix="Set both, or use OM_SEARCH_BACKEND=bm25 to stay fully local",
            )

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

    # 12. Cowork plugin
    cowork_plugin_dir = _cowork_plugin_dir(config)
    if cowork_plugin_dir.exists():
        _check("Cowork plugin", "PASS", str(cowork_plugin_dir))
        hooks_json = cowork_plugin_dir / "hooks" / "hooks.json"
        if hooks_json.exists():
            valid, detail = _validate_cowork_hooks_json(hooks_json)
            if valid:
                _check("Cowork hooks.json", "PASS", detail)
            else:
                _check("Cowork hooks.json", "FAIL", detail, fix="Run: om install --cowork")
        else:
            _check("Cowork hooks.json", "FAIL", "missing", fix="Run: om install --cowork")
        for script_name in ("session-start.sh", "session-end.sh"):
            script = cowork_plugin_dir / "hooks" / "scripts" / script_name
            if not script.exists():
                _check(f"Cowork {script_name}", "FAIL", "missing", fix="Run: om install --cowork")
            elif sys.platform == "win32":
                # POSIX X_OK doesn't apply to Windows; report the script as
                # present but flag that Cowork isn't supported here.
                _check(f"Cowork {script_name}", "WARN", "present but Cowork is not supported on Windows")
            elif os.access(script, os.X_OK):
                _check(f"Cowork {script_name}", "PASS", "executable")
            else:
                _check(f"Cowork {script_name}", "WARN", "not executable", fix=f"Run: chmod +x {script}")
    else:
        _check("Cowork plugin", "WARN", "not installed", fix="Run: om install --cowork")

    # 13. OpenCode plugin and fallback
    opencode_plugin = config.opencode_plugins_dir / _OPENCODE_PLUGIN_NAME
    if opencode_plugin.exists():
        try:
            plugin_text = opencode_plugin.read_text()
            if "opencode-event" in plugin_text:
                _check("OpenCode plugin", "PASS", str(opencode_plugin))
            else:
                _check(
                    "OpenCode plugin",
                    "FAIL",
                    "plugin present but missing opencode-event",
                    fix="Run: om install --opencode",
                )
        except Exception as e:
            _check("OpenCode plugin", "FAIL", f"error reading plugin: {e}", fix="Run: om install --opencode")
    else:
        _check("OpenCode plugin", "WARN", "not installed", fix="Run: om install --opencode")

    if config.opencode_agents_md.exists():
        try:
            agents_text = config.opencode_agents_md.read_text()
            if _OPENCODE_OM_MARKER in agents_text:
                _check("OpenCode AGENTS fallback", "PASS", str(config.opencode_agents_md))
            else:
                _check(
                    "OpenCode AGENTS fallback",
                    "WARN",
                    "AGENTS.md present without OM block",
                    fix="Run: om install --opencode",
                )
        except Exception as e:
            _check(
                "OpenCode AGENTS fallback",
                "WARN",
                f"error reading AGENTS.md: {e}",
                fix="Run: om install --opencode",
            )
    else:
        _check("OpenCode AGENTS fallback", "WARN", "not installed", fix="Run: om install --opencode")

    if config.opencode_events_dir.exists():
        event_logs = list(config.opencode_events_dir.glob("*.jsonl"))
        if event_logs:
            _check("OpenCode event logs", "PASS", f"{len(event_logs)} file(s)")
        else:
            _check("OpenCode event logs", "WARN", "events directory exists but contains no JSONL logs yet")
    else:
        _check("OpenCode event logs", "WARN", "no event logs yet")

    # 14. Kimi Code CLI hooks
    if config.kimi_config_path.exists():
        try:
            kimi_config = config.kimi_config_path.read_text()
            has_block = _KIMI_OM_BLOCK_START in kimi_config and _KIMI_OM_BLOCK_END in kimi_config
            has_context = "context --for kimi" in kimi_config
            has_checkpoint = "kimi-checkpoint" in kimi_config
            if has_block and has_context and has_checkpoint:
                _check("Kimi hooks", "PASS", str(config.kimi_config_path))
            elif has_block:
                _check(
                    "Kimi hooks", "FAIL", "OM block present but commands are incomplete", fix="Run: om install --kimi"
                )
            else:
                _check("Kimi hooks", "WARN", "config present without OM block", fix="Run: om install --kimi")
        except Exception as e:
            _check("Kimi hooks", "WARN", f"error reading config: {e}", fix="Check ~/.kimi/config.toml")
    else:
        _check("Kimi hooks", "WARN", "config.toml not found", fix="Run: om install --kimi")

    if config.kimi_om_events_path.exists():
        from .transcripts.kimi import count_events

        event_count = count_events(config.kimi_om_events_path)
        if event_count:
            _check("Kimi event log", "PASS", f"{event_count} event(s)")
        else:
            _check("Kimi event log", "WARN", f"empty event log at {config.kimi_om_events_path}")
    else:
        _check("Kimi event log", "WARN", "no Kimi events captured yet")

    # 15. Grok Build TUI (Phase 1 hook support + Claude compatibility awareness)
    grok_hook_file = config.grok_hooks_dir / "observational-memory.json"
    if grok_hook_file.exists():
        _check("Grok OM hook file", "PASS", str(grok_hook_file))
    else:
        _check("Grok OM hook file", "WARN", "not installed", fix="Run: om install --grok")

    if _has_om_claude_session_start(config):
        _check(
            "Grok Claude compatibility",
            "PASS",
            "OM hooks in ~/.claude/settings.json (Grok inherits context via compatibility layer)",
        )
    else:
        _check("Grok Claude compatibility", "PASS", "using native ~/.grok/hooks/ (no Claude OM hooks detected)")

    if config.grok_config_path.exists():
        try:
            import tomllib

            with config.grok_config_path.open("rb") as f:
                gdata = tomllib.load(f)
            mem = gdata.get("memory", {})
            enabled = mem.get("enabled", False)
            _check(
                "Grok native memory",
                "PASS",
                f"{'enabled' if enabled else 'disabled'} in {config.grok_config_path} (OM is independent peer)",
            )
        except Exception:
            _check("Grok native memory", "WARN", f"config present at {config.grok_config_path} but unreadable")
    else:
        _check("Grok native memory", "PASS", "no config (disabled by default; OM is peer)")

    # 13. Hook paths valid (only check Claude hook commands that look like file paths, not inline shell commands)
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

    if sys.platform == "win32":
        schtasks_jobs = _schtasks_job_statuses(config)
        installed_tasks = [job for job in schtasks_jobs if job["installed"]]
        missing_tasks = [str(job["key"]) for job in schtasks_jobs if not job["installed"]]
        task_errors = [f"{job['key']}: {job['error']}" for job in schtasks_jobs if job["error"]]
        if installed_tasks:
            if missing_tasks:
                _check(
                    "Scheduled tasks",
                    "WARN",
                    f"{len(installed_tasks)}/{len(schtasks_jobs)} installed; missing: {', '.join(missing_tasks)}",
                    fix="Run: om install",
                )
            else:
                _check("Scheduled tasks", "PASS", f"{len(installed_tasks)}/{len(schtasks_jobs)} installed")
        else:
            _check("Scheduled tasks", "WARN", "no OM scheduled tasks found", fix="Run: om install")
        if task_errors:
            _check("schtasks errors", "WARN", "; ".join(task_errors))
    else:
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

    # 14. Cluster sync diagnostics
    try:
        from .sync.config import cluster_feature_enabled, load_cluster_config
        from .sync.permissions import verify_private_path_owner_only
        from .sync.store import ClusterStore

        cluster_config = load_cluster_config(config)
        if cluster_config is None:
            _check("OM Cluster", "WARN", "not initialized", fix="Run: om cluster init")
        else:
            enabled = cluster_feature_enabled(config)
            _check("OM Cluster", "PASS" if enabled else "WARN", "enabled" if enabled else "configured but disabled")
            key_dir = config.cluster_keys_dir / cluster_config.id
            permission_results = [
                verify_private_path_owner_only(config.cluster_keys_dir, directory=True),
                verify_private_path_owner_only(key_dir, directory=True),
                verify_private_path_owner_only(key_dir / "node.json", directory=False),
                verify_private_path_owner_only(key_dir / "cluster.key", directory=False),
            ]
            failures = [result for result in permission_results if result.status == "FAIL"]
            warnings = [result for result in permission_results if result.status == "WARN"]
            if failures:
                _check(
                    "OM Cluster key permissions",
                    "FAIL",
                    "; ".join(result.detail for result in failures),
                    fix=failures[0].fix,
                )
            elif warnings:
                _check(
                    "OM Cluster key permissions",
                    "WARN",
                    "; ".join(result.detail for result in warnings),
                )
            else:
                _check("OM Cluster key permissions", "PASS", "private key paths are owner-only")
            store = ClusterStore.from_config(config)
            records = store.list_records(include_tombstoned=True)
            _check("OM Cluster local records", "PASS", f"{len(records)} record(s), heads: {store.all_heads()}")
            for transport in cluster_config.transports:
                if transport.type == "filesystem" and transport.path:
                    path = Path(transport.path).expanduser()
                    _check(
                        f"OM Cluster transport {transport.type}",
                        "PASS" if path.exists() else "WARN",
                        str(path),
                        fix=f"Create transport directory: {path}",
                    )
    except Exception as e:
        _check("OM Cluster", "WARN", f"diagnostics failed: {e}")

    # 14b. Usage tracking, budgets, and pricing (host-local; never synced)
    try:
        from .usage import resolve_budgets
        from .usage.pricing import load_pricing

        if config.usage_tracking:
            db = config.usage_db_path
            _check("Usage tracking", "PASS", f"{db} ({'present' if db.exists() else 'not created yet'})")
            budgets = resolve_budgets(config)
            if budgets:
                _check("Usage budgets", "PASS", f"{len(budgets)} configured (default mode: {config.budget_mode})")
            else:
                _check("Usage budgets", "PASS", "none configured", fix="Run: om usage budget")
            pricing = load_pricing(config.pricing_overrides_path)
            detail = f"snapshot {pricing.snapshot_date}"
            if pricing.override_path:
                detail += f" (+override {pricing.override_path})"
            _check("Usage pricing", "PASS", detail)
        else:
            _check(
                "Usage tracking",
                "WARN",
                "disabled (OM_USAGE_TRACKING=0)",
                fix="Set OM_USAGE_TRACKING=1 to record token spend",
            )
    except Exception as e:
        _check("Usage tracking", "WARN", f"could not inspect usage subsystem: {e}")

    # 14c. Memory growth (B0) — pure, read-only measurement (v0.8.0 Gate 6).
    # Informational only; this block must never fail doctor.
    try:
        from .growth import growth_doctor_checks, measure_memory_growth

        for name, status, detail in growth_doctor_checks(measure_memory_growth(config)):
            _check(name, status, detail)
    except Exception as e:
        _check("Memory growth (B0)", "WARN", f"could not measure memory growth: {e}")

    # 15. Platform
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


def _quote_hook_executable(path: str) -> str:
    """Quote *path* for use as the executable in a Claude/Codex hook command.

    cmd.exe treats single quotes as literal characters, so paths with
    spaces (e.g. ``C:\\Program Files\\...`` or ``C:\\Users\\First Last\\...``)
    must be wrapped in double quotes. POSIX shells need POSIX-style
    quoting (``shlex.quote``) instead.
    """
    if sys.platform == "win32":
        if not path:
            return '""'
        if any(ch in path for ch in (" ", "\t", '"')):
            escaped = path.replace('"', '\\"')
            return f'"{escaped}"'
        return path

    import shlex

    return shlex.quote(path)


def _claude_hook_commands() -> tuple[str, str]:
    """Return (session_start_command, checkpoint_command) for Claude Code hooks.

    On Windows we cannot rely on bash + jq, so we point hooks at the ``om``
    CLI directly (``om context`` for SessionStart, ``om claude-checkpoint``
    for the checkpoint events). On POSIX we keep the bash hook scripts that
    have been the production behavior for some time.
    """
    if sys.platform == "win32":
        om_path = _find_om_path() or "om"
        quoted = _quote_hook_executable(om_path)
        return f"{quoted} context", f"{quoted} claude-checkpoint"

    hooks_dir = Path(__file__).parent / "hooks" / "claude"
    return str(hooks_dir / "session-start.sh"), str(hooks_dir / "session-end.sh")


def _grok_hook_commands() -> tuple[str, str]:
    """Return (session_start_command, checkpoint_command) for Grok Build TUI hooks.

    On Windows we register direct ``om`` invocations (``om context`` for SessionStart,
    ``om grok-checkpoint`` for checkpoint events) for robustness, exactly as done
    for Claude Code. On POSIX we use the dedicated bash hook script.
    """
    if sys.platform == "win32":
        om_path = _find_om_path() or "om"
        quoted = _quote_hook_executable(om_path)
        return f"{quoted} context", f"{quoted} grok-checkpoint"

    grok_hooks_dir = Path(__file__).parent / "hooks" / "grok"
    return str(grok_hooks_dir / "session-start.sh"), f"{_find_om_path() or 'om'} grok-checkpoint"


def _install_claude_hooks(config: Config) -> None:
    """Add SessionStart and session checkpoint hooks to ~/.claude/settings.json."""
    import json

    session_start_command, checkpoint_command = _claude_hook_commands()

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
                    "command": session_start_command,
                    "timeout": 15,
                    "statusMessage": "Loading observational memory...",
                }
            ]
        }
    ]

    # SessionEnd hook
    hooks["SessionEnd"] = [
        {"hooks": [{"type": "command", "command": checkpoint_command, "timeout": 60, "async": True}]}
    ]

    # UserPromptSubmit checkpoint hook
    hooks["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": checkpoint_command, "timeout": 5, "async": True}]}
    ]

    # PreCompact checkpoint hook
    hooks["PreCompact"] = [{"hooks": [{"type": "command", "command": checkpoint_command, "timeout": 5, "async": True}]}]

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


# --- Cowork plugin installation ---

_COWORK_PLUGIN_NAME = "observational-memory"
_COWORK_HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact")


def _cowork_plugin_dir(config: Config) -> Path:
    return config.cowork_plugins_dir / _COWORK_PLUGIN_NAME


def _validate_cowork_hooks_json(path: Path) -> tuple[bool, str]:
    """Validate enough of the Cowork plugin hook schema for local diagnostics."""
    import json

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc.msg}"

    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return False, "missing top-level hooks object"

    missing = [event for event in _COWORK_HOOK_EVENTS if not isinstance(hooks.get(event), list)]
    if missing:
        return False, f"missing hook events: {', '.join(missing)}"

    return True, f"{len(_COWORK_HOOK_EVENTS)} event(s) configured"


def _install_cowork_plugin(config: Config) -> None:
    """Copy the bundled Cowork plugin to the local-agent-mode-plugins directory."""
    import shutil

    if sys.platform == "win32":
        # Cowork ships only on macOS today and its bash hook scripts depend
        # on jq + bash. We surface a clear message rather than copy files
        # that cannot execute on Windows.
        click.echo("Cowork plugin install is only supported on macOS; skipping on Windows.")
        return

    source_dir = Path(__file__).parent / "cowork_plugin"
    if not source_dir.exists():
        click.echo("Warning: bundled Cowork plugin not found in package", err=True)
        return

    target_dir = _cowork_plugin_dir(config)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    # Ensure hook scripts are executable (POSIX permission bits).
    scripts_dir = target_dir / "hooks" / "scripts"
    if scripts_dir.exists():
        for script in scripts_dir.glob("*.sh"):
            script.chmod(script.stat().st_mode | 0o755)

    click.echo(f"Installed Cowork plugin to {target_dir}")


def _uninstall_cowork_plugin(config: Config) -> None:
    """Remove the observational-memory Cowork plugin."""
    import shutil

    target_dir = _cowork_plugin_dir(config)
    if target_dir.exists():
        shutil.rmtree(target_dir)
        click.echo(f"Removed Cowork plugin from {target_dir}")
    else:
        click.echo("Cowork plugin not installed, nothing to remove")


# --- Codex integration ---

_CODEX_OM_MARKER = "<!-- observational-memory -->"
_CODEX_OM_FALLBACK_VERSION_MARKER = "<!-- observational-memory:codex-hooks-fallback-v2 -->"
_CODEX_HOOKS_FEATURE_FLAGS = ("hooks", "codex_hooks")
_CODEX_SESSION_START_MATCHER = "startup|resume"
_CODEX_SESSION_START_STATUS = "Loading observational memory..."
_CODEX_STOP_STATUS = "Checkpointing observational memory..."

_CODEX_OM_BLOCK = f"""{_CODEX_OM_MARKER}
## Observational Memory

{_CODEX_OM_FALLBACK_VERSION_MARKER}

Codex startup context is normally injected through hooks.

If this session does not already include sections titled `# Startup Profile` and `# Active Context`,
run the budgeted startup command before substantial work:

`om context --for codex --cwd "$PWD"`

Use the JSON `hookSpecificOutput.additionalContext` text as the startup context.
This keeps startup bounded, de-duplicated, and routed to the current working directory.

If `om context` is unavailable, do not bulk-read generated memory files.
Use `om search "<query>"` first; only inspect `profile.md` or `active.md`
for a narrow fact when search is unavailable too.

If this is a long-lived Codex session, Codex observations run every 15 minutes by default.
To adjust that interval, edit `~/.config/observational-memory/env` and set
`OM_CODEX_OBSERVER_INTERVAL_MINUTES` (for example: `OM_CODEX_OBSERVER_INTERVAL_MINUTES=5`).
You can run a manual checkpoint with `om observe --source codex`.

For deeper context when needed, consult:
- `om recall --query "<query>"`
- `om recall --handle <handle>` for handles emitted by `om context`
- `om search "<query>"`

These files are auto-maintained. Do not modify them directly.

Grok Build TUI (xAI) is supported via `om install --grok` (or `--all`).
Grok uses the same OM profile.md / active.md for startup context
(via native ~/.grok/hooks/observational-memory.json or the Claude compatibility layer).
Use `om grok-checkpoint --transcript <updates.jsonl>` from Grok SessionEnd/UserPromptSubmit hooks,
and `om observe --source grok` to ingest sessions.
The same `om search` / `om recall` / `om context` work from within Grok.

{_CODEX_OM_MARKER}"""


def _build_codex_session_start_command() -> str:
    """Return the command string for the Codex SessionStart hook."""
    om_path = _find_om_path() or "om"
    return f"{_quote_hook_executable(om_path)} context"


def _build_codex_checkpoint_command() -> str:
    """Return the command string for the Codex Stop hook."""
    om_path = _find_om_path() or "om"
    return f"{_quote_hook_executable(om_path)} codex-checkpoint"


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

    if len(parts) != 2 or parts[1] != subcommand:
        return False
    # On Windows, uv installs ``om`` as ``om.exe``; treat the stem as canonical.
    return Path(parts[0]).stem.lower() == "om"


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
                "timeout": 15,
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

    if not agents_md.exists():
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        agents_md.write_text(_CODEX_OM_BLOCK + "\n")
        click.echo(f"Installed Codex AGENTS fallback in {agents_md}")
        return

    existing = agents_md.read_text()
    if _CODEX_OM_MARKER in existing:
        pattern = rf"\n*{re.escape(_CODEX_OM_MARKER)}.*?{re.escape(_CODEX_OM_MARKER)}\n*"
        replaced = re.sub(pattern, "\n\n" + _CODEX_OM_BLOCK + "\n", existing, flags=re.DOTALL)
        agents_md.write_text(replaced.strip() + "\n")
        click.echo(f"Updated observational memory instructions in {agents_md}")
        return

    agents_md.write_text(existing.rstrip() + "\n\n" + _CODEX_OM_BLOCK + "\n")
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


def _count_claude_transcript_messages(transcript: Path) -> int:
    """Return the number of parsed Claude (or Cowork) messages in a transcript."""
    from .transcripts.claude import parse_transcript

    if not transcript.exists():
        return 0

    try:
        return len(parse_transcript(transcript))
    except OSError:
        return 0


def _checkpoint_lock_stale_minutes(default: int = 60) -> int:
    raw_value = os.environ.get("OM_SESSION_OBSERVER_LOCK_STALE_MINUTES", str(default))
    try:
        stale_minutes = int(raw_value)
    except ValueError:
        return default

    return max(stale_minutes, 0)


def _session_observer_interval_seconds(default: int = 900) -> int:
    """Return the in-session checkpoint throttle interval in seconds."""
    raw_value = os.environ.get("OM_SESSION_OBSERVER_INTERVAL_SECONDS", str(default))
    try:
        seconds = int(raw_value)
    except ValueError:
        return default
    return max(seconds, 0)


def _hash_transcript_path(transcript: Path) -> str:
    import hashlib

    return hashlib.sha256(str(transcript).encode("utf-8")).hexdigest()


def _checkpoint_lock_path(lock_dir: Path, transcript: Path) -> Path:
    """Return the lock directory path for one transcript under *lock_dir*."""
    return lock_dir / _hash_transcript_path(transcript)


def _write_checkpoint_lock_owner(lock_path: Path, *, pid: int | None = None) -> None:
    from .sync.atomic import write_lock_owner

    try:
        write_lock_owner(lock_path, pid=pid, created=datetime.now(timezone.utc).timestamp())
    except OSError:
        pass


def _checkpoint_lock_owner_is_dead(lock_path: Path) -> bool:
    from .sync.atomic import lock_owner_process_is_dead

    return lock_owner_process_is_dead(lock_path)


def _checkpoint_lock_should_reclaim(lock_path: Path, stale_minutes: int) -> bool:
    if _checkpoint_lock_owner_is_dead(lock_path):
        return True
    if (lock_path / "owner").exists():
        return False
    if stale_minutes <= 0:
        return False
    try:
        mtime = lock_path.stat().st_mtime
    except OSError:
        return False
    age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - mtime)
    return age_seconds > stale_minutes * 60


def _acquire_checkpoint_lock(lock_dir: Path, lock_path: Path) -> bool:
    """Acquire a best-effort mkdir lock, sweeping stale entries first."""
    stale_minutes = _checkpoint_lock_stale_minutes()
    lock_dir.mkdir(parents=True, exist_ok=True)

    for entry in lock_dir.iterdir():
        if not entry.is_dir():
            continue
        if _checkpoint_lock_should_reclaim(entry, stale_minutes):
            shutil.rmtree(entry, ignore_errors=True)

    try:
        lock_path.mkdir()
        _write_checkpoint_lock_owner(lock_path)
        return True
    except FileExistsError:
        if not lock_path.exists():
            return False

        if not _checkpoint_lock_should_reclaim(lock_path, stale_minutes):
            return False

        shutil.rmtree(lock_path, ignore_errors=True)
        try:
            lock_path.mkdir()
            _write_checkpoint_lock_owner(lock_path)
            return True
        except FileExistsError:
            return False


def _release_checkpoint_lock(lock_path: Path) -> None:
    """Release a checkpoint mkdir lock if it exists."""
    shutil.rmtree(lock_path, ignore_errors=True)


def _load_checkpoint_state(state_path: Path) -> dict[str, dict]:
    """Load checkpoint hook state, tolerating missing or invalid files."""
    import json

    if not state_path.exists():
        return {}

    try:
        payload = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _update_checkpoint_state(
    state_path: Path,
    transcript: Path,
    *,
    message_count: int,
    status: str,
) -> None:
    """Persist the latest checkpoint hook state for one transcript."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        fcntl = None

    import json
    import tempfile

    state_lock_path = state_path.with_suffix(".lock")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_lock_path.open("a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        try:
            state = _load_checkpoint_state(state_path)
            state[str(transcript)] = {
                "last_observed": int(datetime.now(timezone.utc).timestamp()),
                "message_count": max(message_count, 0),
                "status": status,
            }

            with tempfile.NamedTemporaryFile("w", dir=state_path.parent, delete=False) as tmp:
                json.dump(state, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp_path = Path(tmp.name)
            tmp_path.replace(state_path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# --- Codex-specific wrappers (kept for backward-compat with existing imports) ---


def _codex_checkpoint_lock_path(config: Config, transcript: Path) -> Path:
    return _checkpoint_lock_path(config.codex_checkpoint_lock_dir, transcript)


def _codex_checkpoint_lock_stale_minutes(default: int = 60) -> int:
    return _checkpoint_lock_stale_minutes(default)


def _acquire_codex_checkpoint_lock(config: Config, lock_path: Path) -> bool:
    return _acquire_checkpoint_lock(config.codex_checkpoint_lock_dir, lock_path)


def _release_codex_checkpoint_lock(lock_path: Path) -> None:
    _release_checkpoint_lock(lock_path)


def _load_codex_checkpoint_state(config: Config) -> dict[str, dict]:
    return _load_checkpoint_state(config.codex_checkpoint_state_path)


def _update_codex_checkpoint_state(
    config: Config,
    transcript: Path,
    *,
    message_count: int,
    status: str,
) -> None:
    _update_checkpoint_state(
        config.codex_checkpoint_state_path,
        transcript,
        message_count=message_count,
        status=status,
    )


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
        if active_platform == "darwin":
            return "launchd"
        if active_platform == "win32":
            return "schtasks"
        return "cron"

    if requested == "launchd" and active_platform != "darwin":
        raise click.ClickException("--scheduler launchd is only supported on macOS.")

    if requested == "schtasks" and active_platform != "win32":
        raise click.ClickException("--scheduler schtasks is only supported on Windows.")

    if requested == "cron" and active_platform == "win32":
        raise click.ClickException("--scheduler cron is not supported on Windows; use --scheduler schtasks.")

    return requested


def _launchd_domain_target() -> str:
    """Return the per-user GUI domain target for launchctl."""
    if sys.platform != "darwin":
        # launchd targets are macOS-only; os.getuid() doesn't exist on Windows.
        raise click.ClickException("launchd targets are only available on macOS.")
    return f"gui/{os.getuid()}"


def _launchd_service_target(label: str) -> str:
    """Return the fully qualified launchctl service target."""
    return f"{_launchd_domain_target()}/{label}"


def _targets_include_codex_scheduler(targets: str) -> bool:
    """Return whether a target selection should manage the Codex observer backstop."""
    return targets in ("codex", "both", "all")


def _targets_include_claude_scheduler(targets: str) -> bool:
    """Return whether a target selection should manage the Claude observer backstop."""
    return targets in ("claude", "both", "all")


def _targets_include_shared_scheduler(targets: str) -> bool:
    """Return whether a target selection should manage shared auto-memory/reflect jobs."""
    return targets in ("claude", "codex", "both", "all")


def _launchd_job_specs(config: Config, targets: str, om_path: str | None = None) -> list[dict[str, object]]:
    """Return OM-managed launchd job specs for the selected install targets."""
    resolved_om_path = str(Path(om_path).expanduser()) if om_path else None
    specs: list[dict[str, object]] = []

    if _targets_include_codex_scheduler(targets):
        specs.append(
            {
                "key": "codex",
                "label": config.CODEX_OBSERVE_LAUNCHD_LABEL,
                "plist_path": config.codex_observe_launchd_plist_path,
                "argv": [resolved_om_path, "observe-worker", "--source", "codex"] if resolved_om_path else [],
                "run_at_load": True,
                "start_interval": _codex_observer_interval_minutes() * 60,
                "stdout_path": config.codex_observe_launchd_stdout_path,
                "stderr_path": config.codex_observe_launchd_stderr_path,
            }
        )

    if _targets_include_claude_scheduler(targets):
        specs.append(
            {
                "key": "claude",
                "label": config.CLAUDE_OBSERVE_LAUNCHD_LABEL,
                "plist_path": config.claude_observe_launchd_plist_path,
                "argv": [resolved_om_path, "observe-worker", "--source", "claude"] if resolved_om_path else [],
                "run_at_load": True,
                "start_interval": _claude_observer_interval_minutes() * 60,
                "stdout_path": config.claude_observe_launchd_stdout_path,
                "stderr_path": config.claude_observe_launchd_stderr_path,
            }
        )

    if _targets_include_shared_scheduler(targets):
        specs.extend(
            [
                {
                    "key": "claude-memory",
                    "label": config.AUTO_MEMORY_LAUNCHD_LABEL,
                    "plist_path": config.auto_memory_launchd_plist_path,
                    "argv": [resolved_om_path, "observe-worker", "--source", "claude-memory"]
                    if resolved_om_path
                    else [],
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


def _observer_interval_minutes(env_name: str, default: int = 15) -> int:
    raw_interval = os.environ.get(env_name, str(default))
    try:
        interval = int(raw_interval)
    except ValueError:
        click.echo(
            f"Warning: invalid {env_name}={raw_interval!r}; using default {default}.",
            err=True,
        )
        return default

    if interval <= 0:
        click.echo(
            f"Warning: {env_name} must be >0; using default {default}.",
            err=True,
        )
        return default
    return min(interval, 59)


def _codex_observer_interval_minutes(default: int = 15) -> int:
    return _observer_interval_minutes("OM_CODEX_OBSERVER_INTERVAL_MINUTES", default)


def _claude_observer_interval_minutes(default: int = 15) -> int:
    return _observer_interval_minutes("OM_CLAUDE_OBSERVER_INTERVAL_MINUTES", default)


def _cron_every_minutes(minutes: int) -> str:
    if minutes <= 1:
        return "*"
    return f"*/{minutes}"


def _cron_job_key(line: str) -> str | None:
    """Return the OM cron job key for a crontab line, if any."""
    if "om observe --source codex" in line or "om observe-worker --source codex" in line:
        return "codex"
    if "om observe --source claude-memory" in line or "om observe-worker --source claude-memory" in line:
        return "claude-memory"
    if "om observe --source claude" in line or "om observe-worker --source claude" in line:
        return "claude"
    if "om reflect" in line:
        return "reflect"
    return None


def _cron_job_keys_for_targets(targets: str) -> set[str]:
    """Return the OM cron job keys scoped to one install target selection."""
    keys: set[str] = set()
    if _targets_include_shared_scheduler(targets):
        keys.update({"claude-memory", "reflect"})
    if _targets_include_codex_scheduler(targets):
        keys.add("codex")
    if _targets_include_claude_scheduler(targets):
        keys.add("claude")
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
    ordered_jobs = [om_jobs[key] for key in ("codex", "claude", "claude-memory", "reflect") if key in om_jobs]
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
    except OSError as e:
        return None, str(e)
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
    except OSError as e:
        return str(e)
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

    jobs = {}

    if _targets_include_shared_scheduler(targets):
        jobs.update(
            {
                "claude-memory": f"0 * * * * {prefix}{om_path} observe-worker --source claude-memory 2>/dev/null",
                "reflect": f"0 4 * * * {prefix}{om_path} reflect 2>/dev/null",
            }
        )

    if _targets_include_codex_scheduler(targets):
        codex_interval = _cron_every_minutes(_codex_observer_interval_minutes())
        jobs["codex"] = f"{codex_interval} * * * * {prefix}{om_path} observe-worker --source codex 2>/dev/null"

    if _targets_include_claude_scheduler(targets):
        claude_interval = _cron_every_minutes(_claude_observer_interval_minutes())
        jobs["claude"] = f"{claude_interval} * * * * {prefix}{om_path} observe-worker --source claude 2>/dev/null"

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


# --- Windows Task Scheduler installation ---


def _schtasks_job_specs(config: Config, targets: str, om_path: str | None = None) -> list[dict[str, object]]:
    """Return OM-managed Windows scheduled task specs for the selected install targets."""
    resolved_om_path = om_path or _find_om_path() or "om"
    specs: list[dict[str, object]] = []

    if _targets_include_codex_scheduler(targets):
        specs.append(
            {
                "key": "codex",
                "name": config.CODEX_OBSERVE_SCHTASKS_NAME,
                "argv": [resolved_om_path, "observe-worker", "--source", "codex"],
                # Repeat every N minutes for an effectively-unbounded duration.
                "schedule_minutes": _codex_observer_interval_minutes(),
                "schedule_kind": "minute",
            }
        )

    if _targets_include_claude_scheduler(targets):
        specs.append(
            {
                "key": "claude",
                "name": config.CLAUDE_OBSERVE_SCHTASKS_NAME,
                "argv": [resolved_om_path, "observe-worker", "--source", "claude"],
                "schedule_minutes": _claude_observer_interval_minutes(),
                "schedule_kind": "minute",
            }
        )

    if _targets_include_shared_scheduler(targets):
        specs.extend(
            [
                {
                    "key": "claude-memory",
                    "name": config.AUTO_MEMORY_SCHTASKS_NAME,
                    "argv": [resolved_om_path, "observe-worker", "--source", "claude-memory"],
                    "schedule_minutes": 60,
                    "schedule_kind": "minute",
                },
                {
                    "key": "reflect",
                    "name": config.REFLECT_SCHTASKS_NAME,
                    "argv": [resolved_om_path, "reflect"],
                    "schedule_kind": "daily",
                    "schedule_time": "04:00",
                },
            ]
        )
    return specs


def _schtasks_job_keys_for_targets(targets: str) -> set[str]:
    """Return scheduled-task keys scoped to one install target selection."""
    keys: set[str] = set()
    if _targets_include_shared_scheduler(targets):
        keys.update({"claude-memory", "reflect"})
    if _targets_include_codex_scheduler(targets):
        keys.add("codex")
    if _targets_include_claude_scheduler(targets):
        keys.add("claude")
    return keys


def _schtasks_argv_to_command(argv: list[object]) -> str:
    """Quote argv into a single command string for /TR.

    schtasks.exe expects /TR to be a single string. Surround the executable in
    quotes when it contains spaces (common on Windows where Path.home() lives
    under "C:\\Users\\First Last\\..."). Arguments are quoted defensively too.
    """

    def quote(part: str) -> str:
        if not part:
            return '""'
        if any(ch in part for ch in (" ", "\t", '"')):
            escaped = part.replace('"', '\\"')
            return f'"{escaped}"'
        return part

    return " ".join(quote(str(p)) for p in argv)


def _run_schtasks(args: list[str], timeout: int = _SCHEDULER_COMMAND_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run schtasks.exe with the given args. Returns (returncode, stdout, stderr)."""
    import subprocess

    try:
        result = subprocess.run(
            ["schtasks.exe", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 9009, "", "schtasks.exe not found"
    except OSError as e:
        return -1, "", str(e)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"

    return result.returncode, result.stdout or "", result.stderr or ""


def _schtasks_query_task(name: str) -> tuple[bool, str | None]:
    """Return (installed, error). When installed is True, error is None."""
    rc, _stdout, stderr = _run_schtasks(["/Query", "/TN", name])
    if rc == 0:
        return True, None
    detail = (stderr or "").strip()
    if "cannot find" in detail.lower() or "does not exist" in detail.lower() or rc == 1:
        return False, None
    return False, detail or f"exit {rc}"


def _schtasks_create_task(spec: dict[str, object]) -> str | None:
    """Create or replace one OM scheduled task. Returns an error string on failure."""
    name = str(spec["name"])
    argv = list(spec.get("argv", []))
    if not argv:
        return "missing argv"

    tr = _schtasks_argv_to_command(argv)
    base_args = ["/Create", "/F", "/TN", name, "/TR", tr]

    schedule_kind = spec.get("schedule_kind", "minute")
    if schedule_kind == "minute":
        minutes = int(spec.get("schedule_minutes", 60))
        # /SC MINUTE /MO N runs every N minutes; /SC HOURLY for >= 60 keeps
        # downstream display tools happy. Use HOURLY when N is a clean multiple.
        if minutes >= 60 and minutes % 60 == 0:
            args = [*base_args, "/SC", "HOURLY", "/MO", str(minutes // 60)]
        else:
            args = [*base_args, "/SC", "MINUTE", "/MO", str(max(1, minutes))]
    elif schedule_kind == "daily":
        time_of_day = str(spec.get("schedule_time", "04:00"))
        args = [*base_args, "/SC", "DAILY", "/ST", time_of_day]
    else:
        return f"unsupported schedule_kind: {schedule_kind}"

    rc, _stdout, stderr = _run_schtasks(args)
    if rc == 0:
        return None
    return (stderr or "").strip() or f"exit {rc}"


def _schtasks_delete_task(name: str) -> None:
    """Best-effort delete of a scheduled task; missing tasks are not an error."""
    _run_schtasks(["/Delete", "/F", "/TN", name])


def _install_schtasks(config: Config, targets: str) -> None:
    """Install OM-managed scheduled tasks via schtasks.exe on Windows."""
    if sys.platform != "win32":
        raise click.ClickException("schtasks installation is only supported on Windows.")

    om_path = _find_om_path()
    if not om_path:
        click.echo("Warning: 'om' not found in PATH. Scheduled tasks will use 'om' literally.")

    specs = _schtasks_job_specs(config, targets, om_path=om_path)
    installed = 0
    for spec in specs:
        error = _schtasks_create_task(spec)
        if error is None:
            installed += 1
        else:
            click.echo(f"Warning: failed to install scheduled task {spec['name']}: {error}", err=True)

    click.echo(f"Installed {installed} scheduled task(s) via schtasks")


def _uninstall_schtasks(config: Config, targets: str = "both") -> None:
    """Remove OM-managed scheduled tasks via schtasks.exe on Windows."""
    if sys.platform != "win32":
        return

    specs = _schtasks_job_specs(config, targets)
    if not specs:
        return

    for spec in specs:
        installed, _ = _schtasks_query_task(str(spec["name"]))
        if installed:
            _schtasks_delete_task(str(spec["name"]))

    click.echo("Removed scheduled tasks")


def _schtasks_job_statuses(config: Config, targets: str = "both") -> list[dict[str, object]]:
    """Return installed/loaded state for OM-managed scheduled tasks."""
    if sys.platform != "win32":
        return []

    jobs: list[dict[str, object]] = []
    for spec in _schtasks_job_specs(config, targets):
        name = str(spec["name"])
        installed, error = _schtasks_query_task(name)
        jobs.append(
            {
                "key": spec.get("key", name),
                "name": name,
                # Windows tasks are managed by the Task Scheduler service, so
                # "installed" implies "loaded". We keep both fields for parity
                # with the launchd job-status shape.
                "installed": installed,
                "loaded": installed,
                "error": error,
            }
        )
    return jobs


def _run_grok_checkpoint_observer(config: Config, transcript: Path) -> bool:
    from .observe import observe_grok_transcript

    return observe_grok_transcript(transcript, config) is not None


def _run_all_grok_checkpoint_observers(config: Config) -> int:
    from .observe import observe_all_grok

    return len(observe_all_grok(config))


@cli.command(hidden=True, name="grok-checkpoint")
@click.option("--transcript", type=click.Path(path_type=Path), help="Specific Grok updates.jsonl to process")
@click.pass_context
def grok_checkpoint(ctx: click.Context, transcript: Path | None) -> None:
    """Observe Grok session transcripts (for use by SessionEnd/UserPromptSubmit/PreCompact hooks)."""
    config = ctx.obj["config"]

    try:
        if transcript:
            transcript = Path(transcript).expanduser()

            processed = _run_bounded_observer_call(config, _run_grok_checkpoint_observer, config, transcript)
            if processed:
                click.echo(f"Grok checkpoint processed: {transcript}")
            else:
                click.echo(f"No new Grok messages in {transcript}")
        else:
            processed_count = _run_bounded_observer_call(config, _run_all_grok_checkpoint_observers, config)
            click.echo(f"Grok checkpoint: processed {processed_count} sessions")
    except ObserverWorkerBusy:
        click.echo("Grok checkpoint skipped: another background observer is already running.")
    except ObserverWorkerTimeout as e:
        click.echo(f"Grok checkpoint timed out: {e}", err=True)


# --- OpenCode integration ---

_OPENCODE_PLUGIN_NAME = "observational-memory.js"
_OPENCODE_OM_MARKER = "<!-- observational-memory:opencode -->"
_OPENCODE_OM_BLOCK = f"""{_OPENCODE_OM_MARKER}
## Observational Memory for OpenCode

OpenCode loads this global AGENTS.md from ~/.config/opencode. At the start of substantial work, run:

`om context --for opencode --cwd "$PWD"`

Use the returned startup context. Do not bulk-read generated memory files. For targeted history, run:

- `om recall --query "<query>"`
- `om search "<query>"`

The OpenCode plugin installed by OM records message events locally so `om observe --source opencode` can distill them.
{_OPENCODE_OM_MARKER}"""


def _install_opencode(config: Config) -> None:
    import re
    import shutil

    config.opencode_plugins_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent / "hooks" / "opencode" / _OPENCODE_PLUGIN_NAME
    target = config.opencode_plugins_dir / _OPENCODE_PLUGIN_NAME
    shutil.copy2(source, target)
    click.echo(f"Installed OpenCode plugin in {target}")

    agents_md = config.opencode_agents_md
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    if agents_md.exists():
        existing = agents_md.read_text()
        if _OPENCODE_OM_MARKER in existing:
            pattern = rf"\n*{re.escape(_OPENCODE_OM_MARKER)}.*?{re.escape(_OPENCODE_OM_MARKER)}\n*"
            replaced = re.sub(pattern, "\n\n" + _OPENCODE_OM_BLOCK + "\n", existing, flags=re.DOTALL)
            agents_md.write_text(replaced.strip() + "\n")
        else:
            agents_md.write_text(existing.rstrip() + "\n\n" + _OPENCODE_OM_BLOCK + "\n")
    else:
        agents_md.write_text(_OPENCODE_OM_BLOCK + "\n")
    click.echo(f"Installed OpenCode AGENTS fallback in {agents_md}")


def _uninstall_opencode(config: Config) -> None:
    import re

    plugin = config.opencode_plugins_dir / _OPENCODE_PLUGIN_NAME
    if plugin.exists():
        plugin.unlink()
        click.echo(f"Removed OpenCode plugin {plugin}")

    agents_md = config.opencode_agents_md
    if agents_md.exists():
        content = agents_md.read_text()
        if _OPENCODE_OM_MARKER in content:
            pattern = rf"\n*{re.escape(_OPENCODE_OM_MARKER)}.*?{re.escape(_OPENCODE_OM_MARKER)}\n*"
            content = re.sub(pattern, "\n", content, flags=re.DOTALL)
            agents_md.write_text(content.strip() + "\n" if content.strip() else "")
            click.echo("Removed observational memory from OpenCode AGENTS.md")


@cli.command(hidden=True, name="opencode-event")
@click.option("--cwd", type=click.Path(path_type=Path), default=None)
@click.pass_context
def opencode_event(ctx: click.Context, cwd: Path | None) -> None:
    """Append one OpenCode plugin event to the OM-owned JSONL event log."""
    import hashlib
    from datetime import datetime, timezone

    config = ctx.obj["config"]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return
    cwd_text = str(cwd or Path.cwd())
    event = payload.get("event", {})
    session_id = str(event.get("sessionID") or (event.get("session") or {}).get("id") or "default")
    key = hashlib.sha256(f"{cwd_text}:{session_id}".encode()).hexdigest()[:24]
    config.opencode_events_dir.mkdir(parents=True, exist_ok=True)
    path = config.opencode_events_dir / f"{key}.jsonl"
    payload.setdefault("cwd", cwd_text)
    payload.setdefault("received_at", datetime.now(timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# --- Grok Build TUI (xAI) integration (re-applied after restore) ---

_GROK_OM_HOOK_FILE = "observational-memory.json"


def _has_om_claude_session_start(config: Config) -> bool:
    """Return True if OM SessionStart hook is already installed in ~/.claude/settings.json."""
    claude_settings = config.claude_settings_path
    if not claude_settings.exists():
        return False
    try:
        import json as json_mod

        data = json_mod.loads(claude_settings.read_text())
        hooks = data.get("hooks", {})
        for group in hooks.get("SessionStart", []):
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "observational-memory" in cmd or "om context" in cmd or "hooks/claude/session-start" in cmd:
                    return True
    except Exception:
        return False
    return False


def _install_grok(config: Config) -> None:
    """Install Grok Build TUI hooks.

    Respects existing OM Claude hooks via compatibility layer to avoid duplicate
    context injection. Creates native ~/.grok/hooks/observational-memory.json.

    On Windows, commands are registered as direct ``om`` invocations (matching
    the Claude Code strategy) for robustness.
    """
    import json as json_mod

    grok_hooks_dir = config.grok_hooks_dir
    grok_hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_file = grok_hooks_dir / _GROK_OM_HOOK_FILE

    has_claude_om = _has_om_claude_session_start(config)

    hooks_payload: dict[str, list[dict[str, object]]] = {}
    payload: dict[str, object] = {"hooks": hooks_payload}

    session_start_cmd, checkpoint_cmd = _grok_hook_commands()

    if not has_claude_om:
        hooks_payload["SessionStart"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": session_start_cmd,
                        "timeout": 15,
                        "statusMessage": "Loading observational memory...",
                    }
                ]
            }
        ]
        click.echo("Installed Grok SessionStart hook (native)")
    else:
        click.echo("Grok SessionStart omitted (Claude compatibility layer already provides OM context)")

    # Register checkpoint events using the dedicated grok-checkpoint command
    for event in ["SessionEnd", "UserPromptSubmit", "PreCompact"]:
        hooks_payload[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": checkpoint_cmd,
                        "timeout": 30,
                        "async": True,
                        "statusMessage": "Checkpointing observational memory (Grok)...",
                    }
                ]
            }
        ]

    hook_file.write_text(json_mod.dumps(payload, indent=2) + "\n")
    click.echo(f"Installed Grok hooks in {hook_file}")

    if has_claude_om:
        click.echo(
            "Note: Grok will inherit OM context via ~/.claude/settings.json compatibility. Run `om doctor` to verify."
        )


def _uninstall_grok(config: Config) -> None:
    """Remove OM Grok hooks file if it only contains our entries."""
    import json as json_mod

    hook_file = config.grok_hooks_dir / _GROK_OM_HOOK_FILE
    if not hook_file.exists():
        return

    try:
        payload = json_mod.loads(hook_file.read_text())
    except Exception:
        hook_file.unlink(missing_ok=True)
        click.echo(f"Removed invalid Grok hook file {hook_file}")
        return

    hooks = payload.get("hooks", {})
    our_keys = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact"}
    if set(hooks.keys()).issubset(our_keys) or not hooks:
        hook_file.unlink()
        click.echo(f"Removed Grok OM hook file {hook_file}")
    else:
        click.echo(f"Left {hook_file} in place (contains user hooks beyond OM)")


# =============================================================================
# om login / om logout / om auth ...
# =============================================================================


_LOGIN_PROVIDER_CHOICES = ("openai-chatgpt", "xai-oauth")
_API_KEY_TARGETS = ("openai", "anthropic", "xai")


@cli.command()
@click.argument("provider", type=click.Choice(_LOGIN_PROVIDER_CHOICES), required=False)
@click.option(
    "--import",
    "do_import",
    is_flag=True,
    help="Import existing CLI tokens from ~/.codex/ and/or ~/.grok/",
)
@click.option(
    "--api-key",
    "api_key_target",
    type=click.Choice(_API_KEY_TARGETS),
    default=None,
    help="Save an API key for openai|anthropic|xai instead of logging in via OAuth",
)
@click.option("--key", default=None, help="API key value (used with --api-key; prompted if omitted)")
@click.option("--no-browser", is_flag=True, help="Do not open a browser automatically")
@click.option(
    "--manual-paste",
    is_flag=True,
    help="Skip the local loopback listener and paste the callback URL by hand (xAI only)",
)
@click.option(
    "--set-default/--no-set-default",
    default=True,
    help="Set OM_LLM_PROVIDER to the provider you log in to (default: yes)",
)
def login(
    provider: str | None,
    do_import: bool,
    api_key_target: str | None,
    key: str | None,
    no_browser: bool,
    manual_paste: bool,
    set_default: bool,
) -> None:
    """Sign in to a subscription or API-key provider."""
    from .auth import (
        AuthError,
        format_auth_error,
        interactive_picker,
        login_api_key,
        login_import,
        login_openai_chatgpt,
        login_xai_oauth,
    )

    try:
        if do_import:
            login_import(provider=provider)
            return
        if api_key_target is not None:
            login_api_key(api_key_target, key=key)
            return
        if provider is None:
            choice = interactive_picker()
            if choice == "import":
                login_import()
                return
            if choice in _API_KEY_TARGETS:
                login_api_key(choice)
                return
            provider = choice
        if provider == "openai-chatgpt":
            login_openai_chatgpt(open_browser=not no_browser, set_default=set_default)
        elif provider == "xai-oauth":
            login_xai_oauth(open_browser=not no_browser, manual_paste=manual_paste, set_default=set_default)
        else:
            raise click.ClickException(f"Unknown provider: {provider}")
    except AuthError as exc:
        raise click.ClickException(format_auth_error(exc)) from exc


@cli.command()
@click.argument("provider", type=click.Choice(_LOGIN_PROVIDER_CHOICES), required=False)
def logout(provider: str | None) -> None:
    """Clear stored subscription tokens."""
    from .auth import logout as _logout

    removed = _logout(provider)
    if not removed:
        click.echo("No subscription tokens to remove.")
        return
    click.echo("Removed: " + ", ".join(removed))


@cli.group()
def auth() -> None:
    """Inspect or refresh stored auth credentials."""


@auth.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def auth_status_cmd(as_json: bool) -> None:
    """Show stored providers (tokens redacted)."""
    from .auth import auth_status

    auth_status(as_json=as_json)


@auth.command("refresh")
@click.argument("provider", type=click.Choice(_LOGIN_PROVIDER_CHOICES), required=False)
def auth_refresh_cmd(provider: str | None) -> None:
    """Force a token refresh now (diagnostic)."""
    from .auth import AuthError, auth_refresh, format_auth_error

    try:
        auth_refresh(provider)
    except AuthError as exc:
        raise click.ClickException(format_auth_error(exc)) from exc


# --- om usage: token/cost tracking and budgets (host-local) ---

_USAGE_WINDOWS = {"daily": "DAILY", "monthly": "MONTHLY", "session": "SESSION"}


def _usage_since_iso(since: str | None) -> str | None:
    """Convert a YYYY-MM-DD date into a UTC ISO timestamp (start of day)."""
    if not since:
        return None
    try:
        dt = datetime.strptime(since.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise click.ClickException(f"--since must be YYYY-MM-DD: {exc}") from exc
    return dt.isoformat()


def _upsert_env_file(config: Config, updates: dict[str, str | None]) -> None:
    """Insert/replace/remove KEY=value lines in the env file, preserving comments.

    A value of ``None`` removes the key. New keys are appended. The env file is
    created from template first if missing.
    """
    from .config import is_windows

    config.ensure_env_file()
    path = config.env_file
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                value = remaining.pop(key)
                if value is None:
                    continue
                out.append(f"{key}={value}")
                continue
        out.append(line)
    for key, value in remaining.items():
        if value is None:
            continue
        out.append(f"{key}={value}")
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text)
    if not is_windows():
        path.chmod(0o600)


def _budget_env_keys(operation: str | None) -> list[str]:
    prefix = f"{operation.upper()}_" if operation else ""
    keys: list[str] = []
    for win in _USAGE_WINDOWS.values():
        for unit in ("USD", "TOKENS"):
            keys.append(f"OM_BUDGET_{prefix}{win}_{unit}")
    return keys


@cli.group()
def usage() -> None:
    """Inspect LLM token usage, cost, and budgets (host-local, never synced)."""


@usage.command("status")
@click.option("--since", default=None, help="Only count calls on/after this UTC date (YYYY-MM-DD).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def usage_status(ctx: click.Context, since: str | None, as_json: bool) -> None:
    """Show usage totals, budgets, and the active pricing snapshot."""
    from .usage import format_status, status_payload

    config = ctx.obj["config"]
    since_utc = _usage_since_iso(since)
    if as_json:
        click.echo(json.dumps(status_payload(config, since_utc=since_utc), indent=2))
    else:
        click.echo(format_status(config, since_utc=since_utc))


@usage.command("tail")
@click.option("--limit", default=20, show_default=True, type=int, help="How many recent calls to show.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def usage_tail(ctx: click.Context, limit: int, as_json: bool) -> None:
    """List the most recent recorded LLM calls (newest first)."""
    from .usage import format_tail, tail_payload

    config = ctx.obj["config"]
    if as_json:
        click.echo(json.dumps(tail_payload(config, limit=limit), indent=2))
    else:
        click.echo(format_tail(config, limit=limit))


def _validate_cap(label: str, raw: str) -> str:
    """Reject caps that resolve_budgets() would silently drop (unparsable or <= 0).

    Uses the same parser as enforcement so a written budget is always enforceable.
    """
    from .usage.budgets import _parse_number

    parsed = _parse_number(raw)
    if parsed is None or parsed <= 0:
        raise click.ClickException(f"Invalid {label} cap {raw!r}: must be a positive number.")
    return raw


@usage.group("budget", invoke_without_command=True)
@click.pass_context
def usage_budget(ctx: click.Context) -> None:
    """Configure token/dollar budgets. With no subcommand, runs an interactive wizard."""
    if ctx.invoked_subcommand is not None:
        return
    _usage_budget_wizard(ctx.obj["config"])


def _usage_budget_wizard(config: Config) -> None:
    click.echo("Configure an Observational Memory budget (written to your env file).")
    scope = click.prompt("Scope", type=click.Choice(["global", "observer", "reflector"]), default="global")
    window = click.prompt("Window", type=click.Choice(list(_USAGE_WINDOWS)), default="daily")
    usd = click.prompt("USD cap (blank to skip)", default="", show_default=False).strip()
    tokens = click.prompt("Token cap (blank to skip)", default="", show_default=False).strip()
    if not usd and not tokens:
        raise click.ClickException("Set at least one of USD cap / token cap.")
    mode = click.prompt("Enforcement", type=click.Choice(["hard", "soft"]), default="hard")

    if usd:
        usd = _validate_cap("USD", usd)
    if tokens:
        tokens = _validate_cap("token", tokens)

    operation = None if scope == "global" else scope
    prefix = f"{operation.upper()}_" if operation else ""
    win = _USAGE_WINDOWS[window]
    updates: dict[str, str | None] = {}
    if usd:
        key = f"OM_BUDGET_{prefix}{win}_USD"
        updates[key] = usd
        updates[f"{key}_MODE"] = mode
    if tokens:
        key = f"OM_BUDGET_{prefix}{win}_TOKENS"
        updates[key] = tokens
        updates[f"{key}_MODE"] = mode
    _upsert_env_file(config, updates)
    click.echo(f"Wrote {len([k for k in updates if not k.endswith('_MODE')])} budget(s) to {config.env_file}:")
    for key, value in updates.items():
        click.echo(f"  {key}={value}")


@usage_budget.command("set")
@click.option("--operation", type=click.Choice(["observer", "reflector"]), default=None, help="Scope to one operation.")
@click.option("--daily-usd", default=None, help="Daily USD cap.")
@click.option("--monthly-usd", default=None, help="Monthly USD cap.")
@click.option("--session-usd", default=None, help="Per-session USD cap.")
@click.option("--daily-tokens", default=None, help="Daily token cap.")
@click.option("--monthly-tokens", default=None, help="Monthly token cap.")
@click.option("--session-tokens", default=None, help="Per-session token cap.")
@click.option("--soft/--hard", "soft", default=None, help="Enforcement for the budgets set here.")
@click.pass_context
def usage_budget_set(
    ctx: click.Context,
    operation: str | None,
    daily_usd: str | None,
    monthly_usd: str | None,
    session_usd: str | None,
    daily_tokens: str | None,
    monthly_tokens: str | None,
    session_tokens: str | None,
    soft: bool | None,
) -> None:
    """Set one or more budgets non-interactively (writes to the env file)."""
    config = ctx.obj["config"]
    prefix = f"{operation.upper()}_" if operation else ""
    pairs = {
        ("DAILY", "USD"): daily_usd,
        ("MONTHLY", "USD"): monthly_usd,
        ("SESSION", "USD"): session_usd,
        ("DAILY", "TOKENS"): daily_tokens,
        ("MONTHLY", "TOKENS"): monthly_tokens,
        ("SESSION", "TOKENS"): session_tokens,
    }
    updates: dict[str, str | None] = {}
    for (win, unit), value in pairs.items():
        if value is None:
            continue
        key = f"OM_BUDGET_{prefix}{win}_{unit}"
        updates[key] = _validate_cap(f"--{win.lower()}-{unit.lower()}", value)
        if soft is not None:
            updates[f"{key}_MODE"] = "soft" if soft else "hard"
    if not updates:
        raise click.ClickException("Provide at least one cap, e.g. --daily-usd 5.00")
    _upsert_env_file(config, updates)
    for key, value in updates.items():
        click.echo(f"set {key}={value}")


@usage_budget.command("clear")
@click.option(
    "--operation", type=click.Choice(["observer", "reflector"]), default=None, help="Clear one operation's budgets."
)
@click.option("--all", "clear_all", is_flag=True, help="Clear ALL configured budgets.")
@click.pass_context
def usage_budget_clear(ctx: click.Context, operation: str | None, clear_all: bool) -> None:
    """Remove configured budgets from the env file."""
    config = ctx.obj["config"]
    if not operation and not clear_all:
        raise click.ClickException("Specify --operation <op> or --all.")
    targets: list[str] = []
    if clear_all:
        for op in (None, "observer", "reflector"):
            targets += _budget_env_keys(op)
    else:
        targets += _budget_env_keys(operation)
    updates: dict[str, str | None] = {}
    for key in targets:
        updates[key] = None
        updates[f"{key}_MODE"] = None
    _upsert_env_file(config, updates)
    click.echo(f"Cleared budgets for {'all scopes' if clear_all else operation} in {config.env_file}.")


@usage.group("pricing")
def usage_pricing() -> None:
    """Inspect or override per-model pricing (USD per 1M tokens)."""


@usage_pricing.command("show")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def usage_pricing_show(ctx: click.Context, as_json: bool) -> None:
    """Show the effective pricing table (snapshot merged with any override)."""
    from .usage.pricing import load_pricing

    config = ctx.obj["config"]
    pricing = load_pricing(config.pricing_overrides_path)
    if as_json:
        payload = {
            "snapshot_date": pricing.snapshot_date,
            "override_path": str(pricing.override_path) if pricing.override_path else None,
            "models": {
                m: {
                    "input": pricing.rates[m]["input"],
                    "output": pricing.rates[m]["output"],
                    "source": pricing.sources.get(m, "builtin"),
                }
                for m in sorted(pricing.rates)
            },
        }
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Pricing snapshot: {pricing.snapshot_date} (USD per 1M tokens)")
    if pricing.override_path:
        click.echo(f"Override file:    {pricing.override_path}")
    click.echo(f"{'model':<26} {'input':>10} {'output':>10}  source")
    for model in sorted(pricing.rates):
        rate = pricing.rates[model]
        source = pricing.sources.get(model, "builtin")
        click.echo(f"{model:<26} {rate['input']:>10.2f} {rate['output']:>10.2f}  {source}")


@usage_pricing.command("set")
@click.option("--model", required=True, help="Model name (e.g. gpt-5.5).")
@click.option("--input", "input_usd", required=True, type=float, help="USD per 1M input tokens.")
@click.option("--output", "output_usd", required=True, type=float, help="USD per 1M output tokens.")
@click.pass_context
def usage_pricing_set(ctx: click.Context, model: str, input_usd: float, output_usd: float) -> None:
    """Add or update a per-host pricing override."""
    from .usage.pricing import write_override

    config = ctx.obj["config"]
    write_override(config.pricing_overrides_path, model, input_usd, output_usd)
    click.echo(f"set {model}: input={input_usd:g} output={output_usd:g} -> {config.pricing_overrides_path}")


@usage_pricing.command("reset")
@click.pass_context
def usage_pricing_reset(ctx: click.Context) -> None:
    """Remove the per-host pricing override file (revert to the shipped snapshot)."""
    config = ctx.obj["config"]
    path = config.pricing_overrides_path
    if path.exists():
        path.unlink()
        click.echo(f"Removed {path}; using shipped snapshot.")
    else:
        click.echo("No override file; already using the shipped snapshot.")


# --- om jobs: async provider jobs (OpenAI Batch) ---


@cli.group()
def jobs() -> None:
    """Manage async provider jobs (OpenAI Batch for offline reflection)."""


@jobs.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def jobs_list(ctx: click.Context, as_json: bool) -> None:
    """List recorded async jobs (newest first)."""
    from dataclasses import asdict

    from .jobs import ProviderJobStore

    config = ctx.obj["config"]
    records = ProviderJobStore(config.openai_batch_jobs_dir).list()
    if as_json:
        click.echo(json.dumps([asdict(r) for r in records], indent=2))
        return
    if not records:
        click.echo("No async jobs recorded.")
        return
    header = f"{'job_id':<18} {'op':<9} {'status':<10} {'model':<20} {'created (UTC)':<20}"
    click.echo(header)
    click.echo("-" * len(header))
    for r in records:
        created = (r.created_at or "")[:19].replace("T", " ")
        click.echo(f"{r.job_id:<18} {r.operation:<9} {r.status:<10} {(r.model or '')[:20]:<20} {created:<20}")


@jobs.command("poll")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def jobs_poll(ctx: click.Context, as_json: bool) -> None:
    """Poll pending jobs and apply any that have completed."""
    from .jobs import apply_completed_jobs

    config = ctx.obj["config"]
    results = apply_completed_jobs(config)
    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("No pending jobs.")
        return
    for r in results:
        detail = f" — {r['detail']}" if r.get("detail") else ""
        artifact = f" (review: {r['artifact']})" if r.get("artifact") else ""
        click.echo(f"{r['job_id']}: {r['status']}{detail}{artifact}")


@jobs.command("show")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.pass_context
def jobs_show(ctx: click.Context, job_id: str, as_json: bool) -> None:
    """Show one job record."""
    from dataclasses import asdict

    from .jobs import ProviderJobStore

    config = ctx.obj["config"]
    record = ProviderJobStore(config.openai_batch_jobs_dir).load(job_id)
    if record is None:
        raise click.ClickException(f"No job '{job_id}'.")
    if as_json:
        click.echo(json.dumps(asdict(record), indent=2))
        return
    for key, value in asdict(record).items():
        click.echo(f"{key:<20} {value}")


@jobs.command("cancel")
@click.argument("job_id")
@click.pass_context
def jobs_cancel(ctx: click.Context, job_id: str) -> None:
    """Request cancellation of a pending job and mark it cancelled locally."""
    from .jobs import BatchProviderError, cancel_job

    config = ctx.obj["config"]
    try:
        record = cancel_job(config, job_id)
    except BatchProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Job {record.job_id} marked {record.status}." + (f" ({record.error})" if record.error else ""))


def _register_cli_plugins() -> None:
    """Attach out-of-tree command groups (e.g. separately licensed add-ons).

    A plugin publishes a callable under the ``observational_memory.cli_plugins``
    entry-point group; it receives the root Click group and registers its own
    commands (``def register(cli: click.Group) -> None``). Core commands always
    win a name collision, and a broken plugin must never take down the core
    CLI — it degrades to a one-line stderr warning.
    """
    try:
        from importlib.metadata import entry_points

        eps = list(entry_points(group="observational_memory.cli_plugins"))
    except Exception:
        return
    for entry_point in eps:
        try:
            before = dict(cli.commands)
            register = entry_point.load()
            register(cli)
            # Core commands always win: restore anything a plugin shadowed.
            for command_name, command in before.items():
                if cli.commands.get(command_name) is not command:
                    cli.commands[command_name] = command
        except Exception as exc:
            click.echo(f"Warning: om CLI plugin {entry_point.name!r} failed to load: {exc}", err=True)


_register_cli_plugins()

# --- Kimi Code CLI integration ---

_KIMI_OM_BLOCK_START = "# --- observational-memory kimi hooks start ---"
_KIMI_OM_BLOCK_END = "# --- observational-memory kimi hooks end ---"
_KIMI_CHECKPOINT_EVENTS = ("UserPromptSubmit", "SubagentStart", "SubagentStop", "StopFailure")


def _build_kimi_context_command() -> str:
    om_path = _find_om_path() or "om"
    return f"{_quote_hook_executable(om_path)} context --for kimi"


def _build_kimi_checkpoint_command() -> str:
    om_path = _find_om_path() or "om"
    return f"{_quote_hook_executable(om_path)} kimi-checkpoint"


def _build_kimi_observe_worker_command() -> list[str]:
    om_path = _find_om_path() or sys.argv[0] or "om"
    return [om_path, "observe-worker", "--source", "kimi"]


def _kimi_observer_interval_seconds(default: int = 60) -> int:
    raw_value = os.environ.get("OM_KIMI_OBSERVER_INTERVAL_SECONDS", str(default))
    try:
        interval = int(raw_value)
    except ValueError:
        click.echo(
            f"Warning: invalid OM_KIMI_OBSERVER_INTERVAL_SECONDS={raw_value!r}; using default {default}.",
            err=True,
        )
        return default
    if interval < 0:
        click.echo(
            f"Warning: OM_KIMI_OBSERVER_INTERVAL_SECONDS must be >=0; using default {default}.",
            err=True,
        )
        return default
    return interval


def _kimi_observer_now() -> float:
    import time

    return time.time()


def _kimi_observer_state_path(config: Config) -> Path:
    return config.memory_dir / ".kimi-observer-state.json"


def _load_kimi_observer_last_spawn(config: Config) -> float:
    path = _kimi_observer_state_path(config)
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text())
        return float(data.get("last_spawn", 0.0))
    except Exception:
        return 0.0


def _record_kimi_observer_spawn(config: Config, timestamp: float) -> None:
    path = _kimi_observer_state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_spawn": timestamp}, sort_keys=True) + "\n")


def _should_spawn_kimi_observer(config: Config) -> tuple[bool, float]:
    now = _kimi_observer_now()
    interval = _kimi_observer_interval_seconds()
    if interval == 0:
        return True, now
    last_spawn = _load_kimi_observer_last_spawn(config)
    return now - last_spawn >= interval, now


def _queue_kimi_observer(config: Config) -> None:
    should_spawn, now = _should_spawn_kimi_observer(config)
    if not should_spawn:
        return
    try:
        _spawn_detached(_build_kimi_observe_worker_command())
    except OSError as e:
        click.echo(f"Warning: failed to queue Kimi observation: {e}", err=True)
        return
    _record_kimi_observer_spawn(config, now)


def _render_kimi_hooks_block() -> str:
    lines = [
        _KIMI_OM_BLOCK_START,
        "# Managed by `om install --kimi`. Kimi hooks receive JSON on stdin.",
        "[[hooks]]",
        'event = "SessionStart"',
        f'command = "{_toml_basic_string(_build_kimi_context_command())}"',
        "timeout = 15",
        "",
    ]
    checkpoint_command = _toml_basic_string(_build_kimi_checkpoint_command())
    for event in _KIMI_CHECKPOINT_EVENTS:
        lines.extend(
            [
                "[[hooks]]",
                f'event = "{event}"',
                f'command = "{checkpoint_command}"',
                "timeout = 5",
                "",
            ]
        )
    lines.append(_KIMI_OM_BLOCK_END)
    return "\n".join(lines)


def _toml_basic_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _replace_marked_block(content: str, block: str) -> str:
    import re

    pattern = rf"\n*{re.escape(_KIMI_OM_BLOCK_START)}.*?{re.escape(_KIMI_OM_BLOCK_END)}\n*"
    if _KIMI_OM_BLOCK_START in content:
        return re.sub(pattern, "\n\n" + block + "\n", content, flags=re.DOTALL).strip() + "\n"
    return content.rstrip() + ("\n\n" if content.strip() else "") + block + "\n"


def _remove_marked_block(content: str) -> str:
    import re

    pattern = rf"\n*{re.escape(_KIMI_OM_BLOCK_START)}.*?{re.escape(_KIMI_OM_BLOCK_END)}\n*"
    return re.sub(pattern, "\n", content, flags=re.DOTALL).strip() + "\n"


def _install_kimi(config: Config) -> None:
    """Install Kimi Code CLI hooks in ~/.kimi/config.toml."""
    path = config.kimi_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    path.write_text(_replace_marked_block(existing, _render_kimi_hooks_block()))
    click.echo(f"Installed Kimi Code CLI hooks in {path}")


def _uninstall_kimi(config: Config) -> None:
    """Remove OM-managed Kimi Code CLI hooks from ~/.kimi/config.toml."""
    path = config.kimi_config_path
    if not path.exists():
        return
    content = path.read_text()
    if _KIMI_OM_BLOCK_START not in content:
        return
    updated = _remove_marked_block(content)
    if updated.strip():
        path.write_text(updated)
        click.echo(f"Removed OM Kimi hooks from {path}")
    else:
        path.unlink()
        click.echo(f"Removed {path}")


@cli.command(hidden=True, name="kimi-checkpoint")
@click.pass_context
def kimi_checkpoint(ctx: click.Context) -> None:
    """Capture Kimi hook JSON from stdin for later observation."""
    import json as json_mod

    config = ctx.obj["config"]
    raw = sys.stdin.read().strip()
    if not raw:
        return
    try:
        payload = json_mod.loads(raw)
    except json_mod.JSONDecodeError:
        payload = {"hook_event_name": "Unknown", "raw": raw}
    if not isinstance(payload, dict):
        payload = {"hook_event_name": "Unknown", "raw": payload}
    payload["om_captured_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path = config.kimi_om_events_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json_mod.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    _queue_kimi_observer(config)
