"""Privacy-preserving source metadata for cluster records."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from observational_memory.config import Config
from observational_memory.transcripts import Message

from .config import ClusterConfig


def source_metadata(
    *,
    config: Config,
    cluster_config: ClusterConfig,
    messages: list[Message] | None = None,
    source: str | None = None,
    transcript_path: Path | None = None,
) -> dict[str, Any]:
    agent = source or _source_from_messages(messages) or "manual"
    project = _project_slug(transcript_path)
    metadata: dict[str, Any] = {
        "agent": agent,
        "host_alias": cluster_config.node_alias,
    }
    if project:
        metadata["project"] = project
    if transcript_path:
        metadata["transcript_id"] = _hash_value(str(transcript_path))
    cwd = Path.cwd()
    metadata["cwd_hash"] = _hash_value(str(cwd))
    remote_hash = _git_remote_hash(cwd)
    if remote_hash:
        metadata["project_id"] = remote_hash
    return metadata


def namespace_for_event(cluster_config: ClusterConfig, source_event: dict[str, Any]) -> str:
    agent = source_event.get("agent")
    for rule in cluster_config.namespace_rules:
        if rule.source and rule.source != agent:
            continue
        if rule.git_remote_hash and rule.git_remote_hash != source_event.get("project_id"):
            continue
        # Raw local paths are deliberately absent from clear metadata; path rules
        # can be applied by callers before creating records in a later extension.
        if rule.path_contains:
            continue
        return rule.namespace
    return cluster_config.default_namespace


def _source_from_messages(messages: list[Message] | None) -> str | None:
    if not messages:
        return None
    for message in messages:
        if message.source:
            return message.source
    return None


def _project_slug(transcript_path: Path | None) -> str | None:
    if transcript_path is None:
        return None
    parent = transcript_path.parent.name
    return parent[:80] if parent else None


def _hash_value(value: str) -> str:
    return "sha256_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git_remote_hash(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    remote = result.stdout.strip()
    if not remote:
        return None
    return _hash_value(remote)
