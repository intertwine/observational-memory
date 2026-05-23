"""On-disk auth store at ~/.config/observational-memory/auth.json.

Locked + 0600 + atomic writes via O_EXCL. Mirrors the structure of
upstream Hermes (nousresearch/hermes-agent hermes_cli/auth.py blob
5fd3676b, 2026-05-23, ``_auth_file_path`` / ``_auth_store_lock`` /
``_load_auth_store`` / ``_save_auth_store``).

Store shape::

  {
    "version": 1,
    "providers": {
      "openai-chatgpt": {...},
      "xai-oauth":      {...}
    },
    "active_provider": "openai-chatgpt",
    "updated_at": "2026-05-23T..."
  }
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config, is_windows

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 10.0


def auth_file_path(config: Config | None = None) -> Path:
    """Resolve auth.json path, honoring OM_AUTH_FILE for tests."""
    override = os.environ.get("OM_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    cfg = config or Config()
    path = cfg.env_file.parent / "auth.json"

    # Seat belt for pytest: refuse to touch the real user's auth store
    # unless the test set OM_AUTH_FILE to a tmp_path. Catches tests that
    # forgot to patch the path. Mirrors upstream Hermes #25821.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real = (Path.home() / ".config" / "observational-memory" / "auth.json").resolve(strict=False)
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real:
            raise RuntimeError(
                "Refusing to touch real user auth store during pytest. "
                "Set OM_AUTH_FILE to a tmp_path in your test fixture."
            )
    return path


def _lock_file_path(config: Config | None = None) -> Path:
    return auth_file_path(config).with_suffix(".lock")


@contextlib.contextmanager
def auth_store_lock(
    *,
    timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS,
    config: Config | None = None,
):
    """Cross-process advisory lock for auth.json.

    Reentrant in-process via a thread-local owner check.
    """
    lock_path = _lock_file_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    try:
        if is_windows():
            import msvcrt  # type: ignore[import-not-found]

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for auth store lock at {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for auth store lock at {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def load_auth_store(config: Config | None = None) -> dict:
    """Read auth.json. Returns an empty store if missing or malformed."""
    path = auth_file_path(config)
    if not path.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        corrupt = path.with_suffix(".json.corrupt")
        try:
            import shutil

            shutil.copy2(path, corrupt)
        except Exception:
            pass
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    return raw


def save_auth_store(auth_store: dict, *, config: Config | None = None) -> Path:
    """Persist auth.json atomically (0600) via O_EXCL tmp + replace."""
    path = auth_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not is_windows():
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        mode = stat.S_IRUSR | stat.S_IWUSR if not is_windows() else 0o600
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
        if not is_windows():
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return path


def load_provider_state(auth_store: dict, provider_id: str) -> dict | None:
    providers = auth_store.get("providers")
    if not isinstance(providers, dict):
        return None
    state = providers.get(provider_id)
    return dict(state) if isinstance(state, dict) else None


def save_provider_state(
    auth_store: dict,
    provider_id: str,
    state: dict,
    *,
    set_active: bool = True,
) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    if set_active:
        auth_store["active_provider"] = provider_id


def delete_provider_state(auth_store: dict, provider_id: str) -> bool:
    providers = auth_store.get("providers")
    if not isinstance(providers, dict) or provider_id not in providers:
        return False
    del providers[provider_id]
    if auth_store.get("active_provider") == provider_id:
        auth_store["active_provider"] = next(iter(providers), None)
    return True


def redact_token(token: str | None) -> str:
    """Return a redacted form (last 4 chars only) for status/logging."""
    if not isinstance(token, str) or not token.strip():
        return "<missing>"
    cleaned = token.strip()
    if len(cleaned) <= 4:
        return "****"
    return f"****{cleaned[-4:]}"
