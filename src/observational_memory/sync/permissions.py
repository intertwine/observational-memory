"""Platform-aware private path permission checks for OM Cluster."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PermissionCheckResult:
    status: str
    detail: str
    fix: str = ""


def harden_private_path(path: Path, *, directory: bool) -> PermissionCheckResult:
    if sys.platform == "win32":
        return PermissionCheckResult(
            "WARN",
            "Windows ACL owner-only hardening is best-effort; run om doctor on the target machine",
        )
    mode = 0o700 if directory else 0o600
    path.chmod(mode)
    return PermissionCheckResult("PASS", f"{oct(mode)} (owner-only)")


def verify_private_path_owner_only(path: Path, *, directory: bool) -> PermissionCheckResult:
    if not path.exists():
        return PermissionCheckResult("WARN", f"missing: {path}")
    if sys.platform == "win32":
        return PermissionCheckResult(
            "WARN",
            "Windows ACL owner-only verification is not available from this portable check",
        )
    expected = 0o700 if directory else 0o600
    mode = path.stat().st_mode & 0o777
    if mode == expected:
        return PermissionCheckResult("PASS", f"{oct(mode)} (owner-only)")
    return PermissionCheckResult(
        "FAIL",
        f"{path}: {oct(mode)} (expected {oct(expected)})",
        fix=f"Run: chmod {oct(expected)[2:]} {path}",
    )
