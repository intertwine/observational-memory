"""Supported macOS lifecycle for the native-memory bridge.

The bridge service is deliberately separate from the legacy observer scheduler.
All launchctl calls are bounded and injectable so lifecycle tests never mutate a
real user domain.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from observational_memory.config import Config

from .secure_fs import SecureAccessError, SecureRoot

NATIVE_BRIDGE_CONFIG_SCHEMA = "om.native-memory-bridge.config.v1"
NATIVE_BRIDGE_INTERVAL_SECONDS = 15 * 60
NATIVE_BRIDGE_CODEX_ALLOWLIST = ("MEMORY.md", "memory_summary.md")
NATIVE_BRIDGE_LAUNCHD_TIMEOUT_SECONDS = 5
NATIVE_BRIDGE_CONFIG_MAX_BYTES = 64 * 1024

LEGACY_WRITER_LABELS = (
    Config.CODEX_OBSERVE_LAUNCHD_LABEL,
    Config.CLAUDE_OBSERVE_LAUNCHD_LABEL,
    Config.AUTO_MEMORY_LAUNCHD_LABEL,
    Config.REFLECT_LAUNCHD_LABEL,
)


class NativeBridgeLifecycleError(RuntimeError):
    """A fail-closed bridge lifecycle error safe to show to an operator."""


@dataclass(frozen=True)
class NativeBridgeSettings:
    """The complete persisted source selection for scheduled bridge runs."""

    claude_projects: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = validate_claude_projects(self.claude_projects)
        if not normalized:
            raise NativeBridgeLifecycleError("at least one Claude project must be selected")
        object.__setattr__(self, "claude_projects", normalized)

    def payload(self) -> dict[str, object]:
        return {
            "schema": NATIVE_BRIDGE_CONFIG_SCHEMA,
            "claude_projects": list(self.claude_projects),
            "codex_allowlist": list(NATIVE_BRIDGE_CODEX_ALLOWLIST),
            "interval_seconds": NATIVE_BRIDGE_INTERVAL_SECONDS,
        }


@dataclass(frozen=True)
class OwnedFileSnapshot:
    """Exact recoverable state for one user-owned regular file."""

    exists: bool
    data: bytes | None = None
    mode: int | None = None


@dataclass(frozen=True)
class LaunchdState:
    """Observed effective state for one user LaunchAgent."""

    label: str
    installed: bool
    loaded: bool
    override: str
    error: str | None = None


RunLaunchctl = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def validate_claude_projects(projects: Sequence[str]) -> tuple[str, ...]:
    """Validate explicit one-component Claude project directory names."""
    normalized: set[str] = set()
    for raw in projects:
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw.encode("utf-8")) > 255
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
        ):
            raise NativeBridgeLifecycleError("Claude project names must be non-empty directory names")
        path = PurePosixPath(raw)
        if len(path.parts) != 1 or path.name in {"", ".", ".."} or raw != path.name:
            raise NativeBridgeLifecycleError(f"unsafe Claude project directory name: {raw}")
        normalized.add(raw)
    return tuple(sorted(normalized))


def discover_claude_projects(config: Config) -> tuple[dict[str, object], ...]:
    """List eligible local Claude project names without reading memory content."""
    try:
        config.claude_projects_dir.lstat()
    except FileNotFoundError:
        return ()
    try:
        with SecureRoot(config.claude_projects_dir, writable=False) as root:
            candidates: list[dict[str, object]] = []
            for project in root.list_entries():
                try:
                    validate_claude_projects((project,))
                    names = root.list_directory(f"{project}/memory")
                except (FileNotFoundError, SecureAccessError, NativeBridgeLifecycleError):
                    continue
                eligible = []
                for name in names:
                    if name.lower() == "raw_memories.md" or not name.lower().endswith(".md"):
                        continue
                    try:
                        root.inspect_regular_file(f"{project}/memory/{name}")
                    except (FileNotFoundError, SecureAccessError):
                        continue
                    eligible.append(name)
                if eligible:
                    candidates.append({"project": project, "eligible_markdown_files": len(eligible)})
            return tuple(candidates)
    except SecureAccessError as exc:
        raise NativeBridgeLifecycleError(f"Claude projects root failed secure validation: {exc}") from exc


def discover_codex_sources(config: Config) -> tuple[dict[str, object], ...]:
    """Report fixed Codex allowlist presence without reading memory content."""
    codex_root = config.codex_home / "memories"
    try:
        codex_root.lstat()
    except FileNotFoundError:
        names: set[str] = set()
    else:
        try:
            with SecureRoot(codex_root, writable=False) as root:
                names = {
                    filename for filename in NATIVE_BRIDGE_CODEX_ALLOWLIST if root.has_secure_regular_file(filename)
                }
        except SecureAccessError as exc:
            raise NativeBridgeLifecycleError(f"Codex memories root failed secure validation: {exc}") from exc
    return tuple({"filename": filename, "present": filename in names} for filename in NATIVE_BRIDGE_CODEX_ALLOWLIST)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"


def settings_bytes(settings: NativeBridgeSettings) -> bytes:
    return _canonical_json_bytes(settings.payload())


def _parse_settings(raw: bytes) -> NativeBridgeSettings:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeBridgeLifecycleError("native bridge config is not valid JSON") from exc
    expected_keys = {"schema", "claude_projects", "codex_allowlist", "interval_seconds"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise NativeBridgeLifecycleError("native bridge config has an unsupported shape")
    if payload.get("schema") != NATIVE_BRIDGE_CONFIG_SCHEMA:
        raise NativeBridgeLifecycleError("native bridge config schema is unsupported")
    if payload.get("codex_allowlist") != list(NATIVE_BRIDGE_CODEX_ALLOWLIST):
        raise NativeBridgeLifecycleError("native bridge Codex allowlist is not the fixed reviewed allowlist")
    if payload.get("interval_seconds") != NATIVE_BRIDGE_INTERVAL_SECONDS:
        raise NativeBridgeLifecycleError("native bridge cadence must be 15 minutes")
    projects = payload.get("claude_projects")
    if not isinstance(projects, list) or any(not isinstance(value, str) for value in projects):
        raise NativeBridgeLifecycleError("native bridge Claude project selection is invalid")
    settings = NativeBridgeSettings(tuple(projects))
    if raw != settings_bytes(settings):
        raise NativeBridgeLifecycleError("native bridge config is not canonical")
    return settings


def load_settings(config: Config, *, required: bool = True) -> NativeBridgeSettings | None:
    """Read the fixed bridge config through a no-follow private root."""
    config_dir = config.native_bridge_config_dir
    try:
        root_info = config_dir.lstat()
    except FileNotFoundError:
        if required:
            raise NativeBridgeLifecycleError("native bridge is not configured; run `om install --native-bridge`")
        return None
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise NativeBridgeLifecycleError("native bridge config directory must be user-owned mode 0700")
    try:
        with SecureRoot(config_dir, writable=False) as storage:
            raw, _info = storage.read_bytes("config.json", max_bytes=NATIVE_BRIDGE_CONFIG_MAX_BYTES)
    except FileNotFoundError:
        if required:
            raise NativeBridgeLifecycleError("native bridge is not configured; run `om install --native-bridge`")
        return None
    except (SecureAccessError, OSError) as exc:
        raise NativeBridgeLifecycleError(f"native bridge config failed secure validation: {exc}") from exc
    file_info = config.native_bridge_config_path.lstat()
    if stat.S_IMODE(file_info.st_mode) != 0o600:
        raise NativeBridgeLifecycleError("native bridge config file must be mode 0600")
    return _parse_settings(raw)


def write_settings(
    config: Config,
    settings: NativeBridgeSettings,
    *,
    expected_snapshot: OwnedFileSnapshot | None = None,
) -> str:
    """Atomically publish canonical 0600 settings below a 0700 root."""
    payload = settings_bytes(settings)
    try:
        config.native_bridge_config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        with SecureRoot(config.native_bridge_config_dir, writable=True, create=True) as storage:
            with storage.acquire_store_transaction() as transaction:
                if expected_snapshot is not None:
                    _require_owned_file_snapshot(config.native_bridge_config_path, expected_snapshot)
                storage.establish_boundary(("config.json",), transaction=transaction)
                storage.atomic_write_bytes("config.json", payload, transaction=transaction)
    except (SecureAccessError, OSError) as exc:
        raise NativeBridgeLifecycleError(f"could not write private native bridge config: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def snapshot_owned_file(path: Path, *, max_bytes: int = NATIVE_BRIDGE_CONFIG_MAX_BYTES) -> OwnedFileSnapshot:
    """Capture a regular single-link user file without following a leaf symlink."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return OwnedFileSnapshot(False)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
        raise NativeBridgeLifecycleError(f"unsafe managed file: {path}")
    if before.st_size > max_bytes:
        raise NativeBridgeLifecycleError(f"managed file exceeds {max_bytes} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeBridgeLifecycleError(f"could not securely open managed file: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise NativeBridgeLifecycleError(f"managed file changed before read: {path}")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise NativeBridgeLifecycleError(f"managed file was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise NativeBridgeLifecycleError(f"managed file grew during read: {path}")
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise NativeBridgeLifecycleError(f"managed file changed during read: {path}")
        return OwnedFileSnapshot(True, b"".join(chunks), stat.S_IMODE(opened.st_mode))
    finally:
        os.close(descriptor)


def _require_owned_file_snapshot(path: Path, expected: OwnedFileSnapshot) -> OwnedFileSnapshot:
    """Prove one managed leaf still matches the snapshot used to render its update."""
    current = snapshot_owned_file(path, max_bytes=4 * 1024 * 1024)
    if current != expected:
        raise NativeBridgeLifecycleError(f"refusing to overwrite a concurrently changed managed file: {path}")
    return current


def atomic_write_owned_file(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    expected_snapshot: OwnedFileSnapshot | None = None,
) -> str:
    """Atomically replace one managed leaf and return the installed digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_snapshot is None:
        snapshot_owned_file(path, max_bytes=max(len(data), NATIVE_BRIDGE_CONFIG_MAX_BYTES))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("managed file write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = None
        if expected_snapshot is not None:
            _require_owned_file_snapshot(path, expected_snapshot)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise NativeBridgeLifecycleError(f"could not atomically write managed file: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(data).hexdigest()


def restore_owned_file(path: Path, snapshot: OwnedFileSnapshot, *, expected_digest: str | None = None) -> None:
    """Restore a snapshot, refusing to overwrite a concurrent replacement."""
    current = snapshot_owned_file(path, max_bytes=4 * 1024 * 1024)
    if current == snapshot:
        return
    if expected_digest is not None:
        current_digest = hashlib.sha256(current.data or b"").hexdigest() if current.exists else None
        if current_digest != expected_digest:
            raise NativeBridgeLifecycleError(f"refusing to overwrite a concurrently changed managed file: {path}")
    if snapshot.exists:
        atomic_write_owned_file(
            path,
            snapshot.data or b"",
            mode=snapshot.mode or 0o600,
            expected_snapshot=current,
        )
    elif current.exists:
        _require_owned_file_snapshot(path, current)
        path.unlink()


def launchd_plist_bytes(config: Config, om_path: str) -> bytes:
    """Render the fixed production LaunchAgent without provider environment."""
    executable = Path(om_path).expanduser()
    if not executable.is_absolute():
        raise NativeBridgeLifecycleError("native bridge LaunchAgent requires an absolute `om` path")
    payload: dict[str, object] = {
        "Label": config.NATIVE_BRIDGE_LAUNCHD_LABEL,
        "ProgramArguments": [str(executable), "native-bridge-worker"],
        "StartInterval": NATIVE_BRIDGE_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Umask": 0o77,
        "EnvironmentVariables": {
            "XDG_CONFIG_HOME": str(config.env_file.parent.parent),
            "XDG_DATA_HOME": str(config.memory_dir.parent),
            "CODEX_HOME": str(config.codex_home),
        },
        "StandardOutPath": str(config.native_bridge_launchd_stdout_path),
        "StandardErrorPath": str(config.native_bridge_launchd_stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _default_run_launchctl(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        timeout=NATIVE_BRIDGE_LAUNCHD_TIMEOUT_SECONDS,
    )


def _service_target(label: str) -> str:
    if sys.platform != "darwin":
        raise NativeBridgeLifecycleError("the native bridge LaunchAgent is only supported on macOS")
    return f"gui/{os.getuid()}/{label}"


def _domain_target() -> str:
    if sys.platform != "darwin":
        raise NativeBridgeLifecycleError("the native bridge LaunchAgent is only supported on macOS")
    return f"gui/{os.getuid()}"


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[:500]


def _missing_service(result: subprocess.CompletedProcess[str]) -> bool:
    detail = _result_detail(result).lower()
    return "could not find service" in detail or "service could not be found" in detail or "no such process" in detail


def launchd_override(label: str, *, run_launchctl: RunLaunchctl = _default_run_launchctl) -> tuple[str, str | None]:
    """Return disabled, enabled, default, or unknown for one exact label."""
    try:
        result = run_launchctl(["print-disabled", _domain_target()])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", str(exc)
    if result.returncode != 0 or result.stderr:
        return "unknown", _result_detail(result)
    pattern = re.compile(rf'^\s*"{re.escape(label)}"\s*=>\s*(true|false)\s*$', re.MULTILINE)
    matches = pattern.findall(result.stdout)
    if len(matches) > 1:
        return "unknown", "launchctl returned duplicate disabled-state entries"
    if not matches:
        return "default", None
    return ("disabled" if matches[0] == "true" else "enabled"), None


def launchd_loaded(label: str, *, run_launchctl: RunLaunchctl = _default_run_launchctl) -> tuple[bool, str | None]:
    try:
        result = run_launchctl(["print", _service_target(label)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, None
    if _missing_service(result):
        return False, None
    return False, _result_detail(result)


def inspect_launchd(
    config: Config,
    label: str,
    *,
    plist_path: Path | None = None,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> LaunchdState:
    if sys.platform != "darwin":
        return LaunchdState(label, False, False, "unavailable", "launchd is only available on macOS")
    selected_plist = plist_path or config.native_bridge_launchd_plist_path
    override, override_error = launchd_override(label, run_launchctl=run_launchctl)
    loaded, load_error = launchd_loaded(label, run_launchctl=run_launchctl)
    return LaunchdState(label, selected_plist.exists(), loaded, override, override_error or load_error)


def _require_launchctl_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode != 0:
        raise NativeBridgeLifecycleError(f"launchctl {operation} failed: {_result_detail(result)}")


def set_launchd_enabled(label: str, enabled: bool, *, run_launchctl: RunLaunchctl = _default_run_launchctl) -> None:
    operation = "enable" if enabled else "disable"
    try:
        result = run_launchctl([operation, _service_target(label)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeBridgeLifecycleError(f"launchctl {operation} failed: {exc}") from exc
    _require_launchctl_success(result, operation)


def bootout_launchd(label: str, *, run_launchctl: RunLaunchctl = _default_run_launchctl) -> None:
    try:
        result = run_launchctl(["bootout", _service_target(label)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeBridgeLifecycleError(f"launchctl bootout failed: {exc}") from exc
    if result.returncode != 0 and not _missing_service(result):
        raise NativeBridgeLifecycleError(f"launchctl bootout failed: {_result_detail(result)}")


def bootstrap_launchd(plist_path: Path, *, run_launchctl: RunLaunchctl = _default_run_launchctl) -> None:
    try:
        result = run_launchctl(["bootstrap", _domain_target(), str(plist_path)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeBridgeLifecycleError(f"launchctl bootstrap failed: {exc}") from exc
    _require_launchctl_success(result, "bootstrap")


def install_bridge_launchd(
    config: Config,
    om_path: str,
    *,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> None:
    """Atomically replace and activate only the bridge LaunchAgent."""
    if sys.platform != "darwin":
        raise NativeBridgeLifecycleError("`om install --native-bridge` is only supported on macOS (launchd required)")
    config.scheduler_log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    config.scheduler_log_dir.chmod(0o700)
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    payload = launchd_plist_bytes(config, om_path)
    prior_file = snapshot_owned_file(config.native_bridge_launchd_plist_path, max_bytes=1024 * 1024)
    prior_state = inspect_launchd(config, config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    if prior_state.error or prior_state.override == "unknown":
        raise NativeBridgeLifecycleError(
            f"cannot snapshot native bridge LaunchAgent state: {prior_state.error or 'unknown'}"
        )
    installed_digest = hashlib.sha256(payload).hexdigest()
    try:
        atomic_write_owned_file(
            config.native_bridge_launchd_plist_path,
            payload,
            mode=0o600,
            expected_snapshot=prior_file,
        )
        set_launchd_enabled(config.NATIVE_BRIDGE_LAUNCHD_LABEL, True, run_launchctl=run_launchctl)
        bootout_launchd(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
        bootstrap_launchd(config.native_bridge_launchd_plist_path, run_launchctl=run_launchctl)
        loaded, error = launchd_loaded(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
        if not loaded or error:
            raise NativeBridgeLifecycleError(f"native bridge LaunchAgent did not load: {error or 'not loaded'}")
    except Exception as activation_error:
        rollback_errors: list[str] = []
        try:
            bootout_launchd(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
        except Exception as exc:
            rollback_errors.append(str(exc))
        try:
            restore_owned_file(
                config.native_bridge_launchd_plist_path,
                prior_file,
                expected_digest=installed_digest,
            )
        except Exception as exc:
            rollback_errors.append(str(exc))
        try:
            _restore_launchd_override(
                config.NATIVE_BRIDGE_LAUNCHD_LABEL,
                prior_state.override,
                run_launchctl=run_launchctl,
            )
            if prior_state.loaded and prior_file.exists and prior_state.override == "enabled":
                bootstrap_launchd(config.native_bridge_launchd_plist_path, run_launchctl=run_launchctl)
        except Exception as exc:
            rollback_errors.append(str(exc))
        detail = f"native bridge activation failed: {activation_error}"
        if rollback_errors:
            detail += "; rollback incomplete: " + "; ".join(rollback_errors)
        raise NativeBridgeLifecycleError(detail) from activation_error


def disable_bridge_launchd(
    config: Config,
    *,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> None:
    """Persistently disable and unload the bridge while retaining all files."""
    if sys.platform != "darwin":
        raise NativeBridgeLifecycleError("native bridge disable is only supported on macOS")
    set_launchd_enabled(config.NATIVE_BRIDGE_LAUNCHD_LABEL, False, run_launchctl=run_launchctl)
    bootout_launchd(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    loaded, error = launchd_loaded(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    override, override_error = launchd_override(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    if loaded or error or override_error or override != "disabled":
        raise NativeBridgeLifecycleError("native bridge disable could not be verified")


def uninstall_bridge_launchd(
    config: Config,
    *,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> None:
    """Remove only the bridge LaunchAgent; preserve private config and generations."""
    if sys.platform != "darwin":
        raise NativeBridgeLifecycleError("native bridge uninstall is only supported on macOS")
    bootout_launchd(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    snapshot = snapshot_owned_file(config.native_bridge_launchd_plist_path, max_bytes=1024 * 1024)
    if snapshot.exists:
        config.native_bridge_launchd_plist_path.unlink()
    # Clear a persisted disabled override so a later reinstall is not poisoned.
    set_launchd_enabled(config.NATIVE_BRIDGE_LAUNCHD_LABEL, True, run_launchctl=run_launchctl)
    loaded, error = launchd_loaded(config.NATIVE_BRIDGE_LAUNCHD_LABEL, run_launchctl=run_launchctl)
    if loaded or error:
        raise NativeBridgeLifecycleError("native bridge uninstall could not be verified")


def purge_bridge_state(config: Config) -> tuple[Path, ...]:
    """Remove only private bridge config, derived data, receipts, and exact logs."""

    roots = (config.native_bridge_config_dir, config.native_bridge_data_dir)
    logs = (config.native_bridge_launchd_stdout_path, config.native_bridge_launchd_stderr_path)
    existing_roots: list[Path] = []
    existing_logs: list[Path] = []

    # Validate every target before deleting the first byte. These directories
    # are created private by the bridge, so a changed owner, type, or mode is a
    # reason to stop and let the operator inspect the path.
    for path in roots:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NativeBridgeLifecycleError(f"unsafe native bridge purge root: {path}")
        existing_roots.append(path)

    for path in logs:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise NativeBridgeLifecycleError(f"unsafe native bridge purge file: {path}")
        existing_logs.append(path)

    removed: list[Path] = []
    for path in existing_roots:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise NativeBridgeLifecycleError(f"native bridge purge incomplete at {path}: {exc}") from exc
        removed.append(path)
    for path in existing_logs:
        try:
            path.unlink()
        except OSError as exc:
            raise NativeBridgeLifecycleError(f"native bridge purge incomplete at {path}: {exc}") from exc
        removed.append(path)
    return tuple(removed)


def quiesce_legacy_launchd(
    config: Config,
    *,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> dict[str, LaunchdState]:
    """Disable and unload all four legacy writers, returning rollback evidence."""
    snapshots: dict[str, LaunchdState] = {}
    for label in LEGACY_WRITER_LABELS:
        plist_path = config.launch_agents_dir / f"{label}.plist"
        state = inspect_launchd(config, label, plist_path=plist_path, run_launchctl=run_launchctl)
        if state.error or state.override == "unknown":
            raise NativeBridgeLifecycleError(f"cannot snapshot legacy writer {label}: {state.error or 'unknown'}")
        snapshots[label] = state
    completed: list[str] = []
    try:
        for label in LEGACY_WRITER_LABELS:
            set_launchd_enabled(label, False, run_launchctl=run_launchctl)
            bootout_launchd(label, run_launchctl=run_launchctl)
            completed.append(label)
    except Exception:
        restore_launchd_states(config, {label: snapshots[label] for label in completed}, run_launchctl=run_launchctl)
        raise
    return snapshots


def _restore_launchd_override(
    label: str,
    override: str,
    *,
    run_launchctl: RunLaunchctl,
) -> None:
    """Restore only explicit enablement; unknown/default states fall back to safe hold."""
    if override == "enabled":
        set_launchd_enabled(label, True, run_launchctl=run_launchctl)
    elif override in {"disabled", "default", "unknown"}:
        set_launchd_enabled(label, False, run_launchctl=run_launchctl)
    else:
        raise NativeBridgeLifecycleError(f"unsupported LaunchAgent override state for {label}: {override}")


def restore_launchd_states(
    config: Config,
    states: dict[str, LaunchdState],
    *,
    run_launchctl: RunLaunchctl = _default_run_launchctl,
) -> None:
    """Restore explicit enablement; keep default/unknown states safely disabled."""
    errors: list[str] = []
    for label, state in states.items():
        try:
            bootout_launchd(label, run_launchctl=run_launchctl)
            _restore_launchd_override(label, state.override, run_launchctl=run_launchctl)
            plist_path = config.launch_agents_dir / f"{label}.plist"
            if state.loaded and state.installed and state.override == "enabled" and plist_path.exists():
                bootstrap_launchd(plist_path, run_launchctl=run_launchctl)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise NativeBridgeLifecycleError("could not restore LaunchAgent state: " + "; ".join(errors))
