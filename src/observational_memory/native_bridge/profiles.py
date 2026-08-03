"""Fixed resource profiles for the native-memory bridge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeResourceProfile:
    name: str
    allowed_pressure: tuple[str, ...]
    timeout_seconds: int
    max_rss_bytes: int
    max_total_input_bytes: int
    max_file_input_bytes: int
    codex_allowlist: tuple[str, ...]
    max_claude_projects: int | None
    requires_temporary_output: bool
    manual_only: bool


STRICT_DEFAULT_PROFILE = BridgeResourceProfile(
    name="strict-default",
    allowed_pressure=("normal",),
    timeout_seconds=15,
    max_rss_bytes=128 * 1024 * 1024,
    max_total_input_bytes=16 * 1024 * 1024,
    max_file_input_bytes=2 * 1024 * 1024,
    codex_allowlist=("MEMORY.md", "memory_summary.md"),
    max_claude_projects=None,
    requires_temporary_output=False,
    manual_only=False,
)

TEMPORARY_LIGHT_CANARY_PROFILE = BridgeResourceProfile(
    name="temporary-light-canary",
    allowed_pressure=("normal", "warning"),
    timeout_seconds=15,
    max_rss_bytes=128 * 1024 * 1024,
    max_total_input_bytes=4 * 1024 * 1024,
    max_file_input_bytes=1 * 1024 * 1024,
    codex_allowlist=("memory_summary.md",),
    max_claude_projects=1,
    requires_temporary_output=True,
    manual_only=True,
)

_PROFILES = {
    STRICT_DEFAULT_PROFILE.name: STRICT_DEFAULT_PROFILE,
    TEMPORARY_LIGHT_CANARY_PROFILE.name: TEMPORARY_LIGHT_CANARY_PROFILE,
}


def get_resource_profile(name: str) -> BridgeResourceProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown bridge resource profile: {name}") from exc
