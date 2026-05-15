"""Cluster sync configuration and local secret loading."""

from __future__ import annotations

import json
import os
import socket
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from observational_memory.config import Config

from .atomic import atomic_write_text
from .crypto import (
    ClusterSecret,
    EncryptedPayload,
    NodeKeypair,
    b64url_decode,
    b64url_encode,
    decrypt_payload,
    encrypt_payload,
    generate_cluster_secret,
    generate_node_keypair,
    sign_ed25519,
    verify_ed25519,
)
from .ids import validate_cluster_id, validate_invite_id, validate_join_request_id, validate_key_id, validate_node_id
from .permissions import harden_private_path
from .records import canonical_json_bytes


@dataclass(frozen=True)
class TransportConfig:
    type: str
    path: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {"type": self.type}
        if self.path:
            data["path"] = self.path
        return data


@dataclass(frozen=True)
class NamespaceRule:
    source: str | None = None
    path_contains: str | None = None
    git_remote_hash: str | None = None
    namespace: str = "personal"
    local_only: bool = False


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool
    id: str
    name: str
    default_namespace: str
    node_id: str
    node_alias: str
    transports: list[TransportConfig] = field(default_factory=list)
    sync_on_observe: bool = False
    sync_on_reflect: bool = False
    sync_before_context: bool = False
    startup_pull_deadline_ms: int = 1500
    background_interval_seconds: int = 300
    namespace_rules: list[NamespaceRule] = field(default_factory=list)


@dataclass(frozen=True)
class InviteToken:
    body: dict[str, Any]
    signature: str
    request_secret_b64: str | None = None


_FEATURE_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def cluster_feature_enabled(config: Config) -> bool:
    """Return True only when a valid initialized cluster exists and is enabled."""
    cache_key = (
        str(config.cluster_config_path),
        str(config.cluster_keys_dir),
        str(config.clusters_dir),
    )
    env_signature = _feature_env_signature()
    config_signature = _path_signature(config.cluster_config_path)
    cached = _FEATURE_CACHE.get(cache_key)
    if (
        cached
        and cached.get("env_signature") == env_signature
        and cached.get("config_signature") == config_signature
        and cached.get("key_signatures") == _cached_key_signatures(cached)
    ):
        return bool(cached["enabled"])

    key_paths: list[Path] = []
    enabled = False
    cluster_config = load_cluster_config(config)
    if cluster_config is None:
        _store_feature_cache(cache_key, env_signature, config_signature, key_paths, False)
        return False

    key_dir = _cluster_key_dir(config, cluster_config.id)
    key_paths = [key_dir / "node.json", key_dir / "cluster.key"]
    try:
        enabled = cluster_config.enabled
        override = os.environ.get("OM_CLUSTER_ENABLED")
        if override is not None:
            enabled = override.strip().lower() in {"1", "true", "yes", "on"}
        if enabled:
            load_node_keypair(config, cluster_config)
            load_cluster_secret(config, cluster_config.id)
            if not (config.clusters_dir / cluster_config.id).exists():
                enabled = False
            else:
                enabled = True
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        enabled = False
    _store_feature_cache(cache_key, env_signature, config_signature, key_paths, enabled)
    return enabled


def clear_cluster_feature_cache() -> None:
    """Clear the cluster feature-gate cache for tests and config-mutating commands."""
    _FEATURE_CACHE.clear()


def load_cluster_config(config: Config) -> ClusterConfig | None:
    path = config.cluster_config_path
    if not path.exists():
        return None
    raw = tomllib.loads(path.read_text())
    cluster = raw.get("cluster", {})
    node = raw.get("node", {})
    namespaces = raw.get("namespaces", {})
    transports = [
        TransportConfig(type=str(item.get("type", "")), path=item.get("path"))
        for item in raw.get("transport", [])
        if item.get("type")
    ]
    rules = [
        NamespaceRule(
            source=item.get("source"),
            path_contains=item.get("path_contains"),
            git_remote_hash=item.get("git_remote_hash"),
            namespace=item.get("namespace", namespaces.get("default", cluster.get("default_namespace", "personal"))),
            local_only=bool(item.get("local_only", False)),
        )
        for item in raw.get("namespace_rule", [])
    ]
    cluster_id = os.environ.get("OM_CLUSTER_ID") or str(cluster.get("id", ""))
    if cluster_id:
        validate_cluster_id(cluster_id)
    node_id = str(node.get("id", ""))
    if node_id:
        validate_node_id(node_id)
    default_namespace = os.environ.get("OM_CLUSTER_DEFAULT_NAMESPACE") or str(
        namespaces.get("default", cluster.get("default_namespace", "personal"))
    )
    return ClusterConfig(
        enabled=_env_or_bool("OM_CLUSTER_ENABLED", bool(cluster.get("enabled", False))),
        id=cluster_id,
        name=str(cluster.get("name", cluster_id or "OM Cluster")),
        default_namespace=default_namespace,
        node_id=node_id,
        node_alias=str(node.get("alias", socket.gethostname() or "local")),
        transports=transports,
        sync_on_observe=_env_or_bool("OM_CLUSTER_SYNC_ON_OBSERVE", bool(cluster.get("sync_on_observe", False))),
        sync_on_reflect=_env_or_bool("OM_CLUSTER_SYNC_ON_REFLECT", bool(cluster.get("sync_on_reflect", False))),
        sync_before_context=_env_or_bool(
            "OM_CLUSTER_SYNC_BEFORE_CONTEXT",
            bool(cluster.get("sync_before_context", False)),
        ),
        startup_pull_deadline_ms=int(
            os.environ.get("OM_CLUSTER_STARTUP_PULL_DEADLINE_MS") or cluster.get("startup_pull_deadline_ms", 1500)
        ),
        background_interval_seconds=int(cluster.get("background_interval_seconds", 300)),
        namespace_rules=rules,
    )


def write_cluster_config(config: Config, cluster_config: ClusterConfig) -> None:
    lines = [
        "[cluster]",
        f"enabled = {_toml_bool(cluster_config.enabled)}",
        f'id = "{_toml_escape(cluster_config.id)}"',
        f'name = "{_toml_escape(cluster_config.name)}"',
        f'default_namespace = "{_toml_escape(cluster_config.default_namespace)}"',
        f"sync_on_observe = {_toml_bool(cluster_config.sync_on_observe)}",
        f"sync_on_reflect = {_toml_bool(cluster_config.sync_on_reflect)}",
        f"sync_before_context = {_toml_bool(cluster_config.sync_before_context)}",
        f"startup_pull_deadline_ms = {cluster_config.startup_pull_deadline_ms}",
        f"background_interval_seconds = {cluster_config.background_interval_seconds}",
        "",
        "[node]",
        f'id = "{_toml_escape(cluster_config.node_id)}"',
        f'alias = "{_toml_escape(cluster_config.node_alias)}"',
        'allowed_sources = ["claude", "codex", "cowork", "hermes", "claude-memory", "manual"]',
        "",
        "[security]",
        "encrypt_records = true",
        "sign_records = true",
        "allow_untrusted_transports = true",
        "",
        "[merge]",
        'observations = "append-only"',
        'reflections = "frontier-snapshot"',
        'profile = "derived-with-overrides"',
        'active = "derived"',
        'redactions = "tombstone"',
        "",
        "[namespaces]",
        f'default = "{_toml_escape(cluster_config.default_namespace)}"',
        "",
    ]
    for transport in cluster_config.transports:
        lines.extend(["[[transport]]", f'type = "{_toml_escape(transport.type)}"'])
        if transport.path:
            lines.append(f'path = "{_toml_escape(transport.path)}"')
        lines.append("")
    for rule in cluster_config.namespace_rules:
        lines.append("[[namespace_rule]]")
        if rule.source:
            lines.append(f'source = "{_toml_escape(rule.source)}"')
        if rule.path_contains:
            lines.append(f'path_contains = "{_toml_escape(rule.path_contains)}"')
        if rule.git_remote_hash:
            lines.append(f'git_remote_hash = "{_toml_escape(rule.git_remote_hash)}"')
        if rule.local_only:
            lines.append("local_only = true")
        lines.append(f'namespace = "{_toml_escape(rule.namespace)}"')
        lines.append("")
    atomic_write_text(config.cluster_config_path, "\n".join(lines).rstrip() + "\n", mode=0o600)


def initialize_cluster_config(
    config: Config,
    *,
    name: str,
    node_alias: str | None = None,
    default_namespace: str = "personal",
    transports: list[TransportConfig] | None = None,
    force: bool = False,
) -> ClusterConfig:
    if config.cluster_config_path.exists() and not force:
        raise FileExistsError(f"Cluster config already exists: {config.cluster_config_path}")

    secret = generate_cluster_secret()
    keypair = generate_node_keypair(alias=node_alias)
    cluster_config = ClusterConfig(
        enabled=True,
        id=secret.cluster_id,
        name=name,
        default_namespace=default_namespace,
        node_id=keypair.node_id,
        node_alias=node_alias or socket.gethostname() or keypair.node_id,
        transports=transports or [],
    )
    write_cluster_config(config, cluster_config)
    write_node_keypair(config, cluster_config.id, keypair)
    write_cluster_secret(config, secret)
    (config.clusters_dir / cluster_config.id).mkdir(parents=True, exist_ok=True)
    return cluster_config


def load_node_keypair(config: Config, cluster_config: ClusterConfig) -> NodeKeypair:
    validate_cluster_id(cluster_config.id)
    path = _secure_cluster_key_dir(config, cluster_config.id) / "node.json"
    raw = json.loads(path.read_text())
    validate_node_id(raw["node_id"])
    if "encryption_private_key_b64" not in raw or "encryption_public_key_b64" not in raw:
        replacement = generate_node_keypair(alias=raw.get("alias"))
        raw["encryption_private_key_b64"] = replacement.encryption_private_key_b64
        raw["encryption_public_key_b64"] = replacement.encryption_public_key_b64
        atomic_write_text(path, json.dumps(raw, indent=2, sort_keys=True) + "\n", mode=0o600)
    return NodeKeypair(
        node_id=raw["node_id"],
        signing_private_key_b64=raw["signing_private_key_b64"],
        signing_public_key_b64=raw["signing_public_key_b64"],
        encryption_private_key_b64=raw.get("encryption_private_key_b64"),
        encryption_public_key_b64=raw.get("encryption_public_key_b64"),
        alias=raw.get("alias"),
    )


def write_node_keypair(config: Config, cluster_id: str, keypair: NodeKeypair) -> None:
    validate_cluster_id(cluster_id)
    validate_node_id(keypair.node_id)
    path = _secure_cluster_key_dir(config, cluster_id) / "node.json"
    data = {
        "node_id": keypair.node_id,
        "alias": keypair.alias,
        "signing_private_key_b64": keypair.signing_private_key_b64,
        "signing_public_key_b64": keypair.signing_public_key_b64,
        "encryption_private_key_b64": keypair.encryption_private_key_b64,
        "encryption_public_key_b64": keypair.encryption_public_key_b64,
    }
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def load_cluster_secret(config: Config, cluster_id: str) -> ClusterSecret:
    validate_cluster_id(cluster_id)
    path = _secure_cluster_key_dir(config, cluster_id) / "cluster.key"
    raw = json.loads(path.read_text())
    validate_cluster_id(raw["cluster_id"])
    if "data_keys" in raw:
        validate_key_id(raw["active_key_id"])
        for key_id in raw["data_keys"]:
            validate_key_id(key_id)
        return ClusterSecret(
            cluster_id=raw["cluster_id"],
            data_keys=dict(raw["data_keys"]),
            active_key_id=raw["active_key_id"],
            active_key_hlc=raw.get("active_key_hlc"),
        )
    return ClusterSecret.single(cluster_id=raw["cluster_id"], data_key_b64=raw["data_key_b64"])


def write_cluster_secret(config: Config, secret: ClusterSecret) -> None:
    validate_cluster_id(secret.cluster_id)
    validate_key_id(secret.active_key_id)
    for key_id in secret.data_keys:
        validate_key_id(key_id)
    path = _secure_cluster_key_dir(config, secret.cluster_id) / "cluster.key"
    data = {
        "cluster_id": secret.cluster_id,
        "active_key_id": secret.active_key_id,
        "active_key_hlc": secret.active_key_hlc,
        "data_keys": secret.data_keys,
    }
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def create_invite_token(
    config: Config,
    cluster_config: ClusterConfig,
    *,
    expires: str = "10m",
    mode: str = "trusted-direct",
) -> str:
    if mode not in {"request", "trusted-direct"}:
        raise ValueError(f"Unsupported invite mode: {mode}")
    keypair = load_node_keypair(config, cluster_config)
    secret = load_cluster_secret(config, cluster_config.id)
    expires_at = datetime.now(timezone.utc) + _parse_duration(expires)
    invite_id = "invite_" + os.urandom(12).hex()
    body = {
        "version": 1,
        "mode": mode,
        "cluster_id": cluster_config.id,
        "cluster_name": cluster_config.name,
        "default_namespace": cluster_config.default_namespace,
        "issuer_node_id": cluster_config.node_id,
        "issuer_alias": cluster_config.node_alias,
        "issuer_signing_public_key_b64": keypair.signing_public_key_b64,
        "issuer_encryption_public_key_b64": keypair.encryption_public_key_b64,
        "transports": [transport.to_dict() for transport in cluster_config.transports],
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "invite_id": invite_id,
    }
    request_secret_b64 = None
    if mode == "trusted-direct":
        body.update(
            {
                "data_keys": secret.data_keys,
                "active_key_id": secret.active_key_id,
                "active_key_hlc": secret.active_key_hlc,
            }
        )
    else:
        request_secret_b64 = b64url_encode(os.urandom(32))
        _write_issued_request_invite(
            config,
            cluster_config.id,
            invite_id,
            {
                "version": 1,
                "invite_id": invite_id,
                "cluster_id": cluster_config.id,
                "request_secret_b64": request_secret_b64,
                "expires_at": body["expires_at"],
            },
        )
    validate_invite_id(body["invite_id"])
    signature = sign_ed25519(keypair.signing_private_key_b64, canonical_json_bytes(body))
    token = {"body": body, "signature": signature}
    if request_secret_b64 is not None:
        token["request_secret_b64"] = request_secret_b64
    return "omc1:" + b64url_encode(canonical_json_bytes(token))


def parse_invite_token(token: str) -> InviteToken:
    """Parse and syntax-check an invite token.

    The signature check here proves the token body is internally consistent.
    It does not establish cluster trust by itself; imported membership records
    still have to chain to a locally trusted issuer node.
    """
    if not token.startswith("omc1:"):
        raise ValueError("Invite token must start with omc1:")
    raw = json.loads(b64url_decode(token.split(":", 1)[1]).decode("utf-8"))
    body = raw["body"]
    signature = raw["signature"]
    request_secret_b64 = raw.get("request_secret_b64")
    validate_cluster_id(body["cluster_id"])
    validate_node_id(body["issuer_node_id"])
    validate_invite_id(body["invite_id"])
    mode = body.get("mode", "trusted-direct")
    if mode not in {"request", "trusted-direct"}:
        raise ValueError(f"Unsupported invite mode: {mode}")
    if mode == "trusted-direct":
        validate_key_id(body["active_key_id"])
        for key_id in body.get("data_keys", {}):
            validate_key_id(key_id)
    elif not isinstance(request_secret_b64, str):
        raise ValueError("Request-mode invite is missing local approval secret")
    if not verify_ed25519(body["issuer_signing_public_key_b64"], canonical_json_bytes(body), signature):
        raise ValueError("Invite token signature is invalid")
    expires_at = _parse_iso_z(body["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise ValueError("Invite token has expired")
    return InviteToken(body=body, signature=signature, request_secret_b64=request_secret_b64)


def join_cluster_from_invite(
    config: Config,
    token: str,
    *,
    node_alias: str | None = None,
    force: bool = False,
) -> tuple[ClusterConfig, dict[str, Any]]:
    if config.cluster_config_path.exists() and not force:
        raise FileExistsError(f"Cluster config already exists: {config.cluster_config_path}")
    invite = parse_invite_token(token)
    body = invite.body
    keypair = generate_node_keypair(alias=node_alias)
    mode = body.get("mode", "trusted-direct")
    cluster_config = ClusterConfig(
        enabled=True,
        id=body["cluster_id"],
        name=body["cluster_name"],
        default_namespace=body.get("default_namespace", "personal"),
        node_id=keypair.node_id,
        node_alias=node_alias or socket.gethostname() or keypair.node_id,
        transports=[TransportConfig(**transport) for transport in body.get("transports", [])],
    )
    write_cluster_config(config, cluster_config)
    write_node_keypair(config, cluster_config.id, keypair)
    _write_inviter_public_metadata(config, cluster_config.id, body)
    invite_payload = {"body": body, "signature": invite.signature}
    if mode == "trusted-direct":
        secret = ClusterSecret(
            cluster_id=body["cluster_id"],
            data_keys=dict(body["data_keys"]),
            active_key_id=body["active_key_id"],
            active_key_hlc=body.get("active_key_hlc"),
        )
        write_cluster_secret(config, secret)
        return cluster_config, invite_payload
    if not invite.request_secret_b64:
        raise ValueError("Request-mode invite is missing local approval secret")

    request_id = "join_" + os.urandom(12).hex()
    requested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    request_body = {
        "version": 1,
        "cluster_id": cluster_config.id,
        "request_id": request_id,
        "requested_at": requested_at,
        "expires_at": body["expires_at"],
        "invite_id": body["invite_id"],
        "invite": invite_payload,
        "node": {
            "node_id": keypair.node_id,
            "alias": cluster_config.node_alias,
            "signing_public_key_b64": keypair.signing_public_key_b64,
            "encryption_public_key_b64": keypair.encryption_public_key_b64,
        },
        "requested_namespaces": [cluster_config.default_namespace],
    }
    validate_join_request_id(request_id)
    request_signature = sign_ed25519(keypair.signing_private_key_b64, canonical_json_bytes(request_body))
    join_request = {**request_body, "signature": request_signature}
    _write_pending_join_state(
        config,
        cluster_config.id,
        {
            "version": 1,
            "status": "pending",
            "request_id": request_id,
            "cluster_id": cluster_config.id,
            "invite_secret_b64": invite.request_secret_b64,
            "invite": invite_payload,
            "join_request": join_request,
        },
    )
    return cluster_config, {**invite_payload, "join_request": join_request}


def load_pending_join_state(config: Config, cluster_id: str | None = None) -> dict[str, Any] | None:
    cluster_config = load_cluster_config(config)
    resolved_cluster_id = cluster_id or (cluster_config.id if cluster_config else "")
    if not resolved_cluster_id:
        return None
    path = _pending_join_state_path(config, resolved_cluster_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def verify_join_request(request: dict[str, Any], *, cluster_id: str) -> dict[str, Any]:
    validate_cluster_id(cluster_id)
    if request.get("version") != 1:
        raise ValueError("Unsupported join request version")
    if request.get("cluster_id") != cluster_id:
        raise ValueError("Join request cluster_id mismatch")
    validate_join_request_id(request["request_id"])
    node = request.get("node")
    if not isinstance(node, dict):
        raise ValueError("Join request missing node")
    validate_node_id(node["node_id"])
    signature = request.get("signature")
    if not isinstance(signature, str):
        raise ValueError("Join request missing signature")
    body = dict(request)
    body.pop("signature", None)
    if not verify_ed25519(node["signing_public_key_b64"], canonical_json_bytes(body), signature):
        raise ValueError("Join request signature is invalid")
    invite = request.get("invite")
    if not isinstance(invite, dict):
        raise ValueError("Join request missing invite")
    invite_token = InviteToken(body=invite["body"], signature=invite["signature"])
    _verify_invite_payload(invite_token)
    if invite_token.body.get("mode") != "request":
        raise ValueError("Join request invite is not request mode")
    if invite_token.body.get("invite_id") != request.get("invite_id"):
        raise ValueError("Join request invite_id mismatch")
    expires_at = _parse_iso_z(request["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise ValueError("Join request has expired")
    return request


def create_join_approval(
    config: Config,
    cluster_config: ClusterConfig,
    *,
    request: dict[str, Any],
    membership_record_id: str,
    approved_by_node_id: str,
) -> dict[str, Any]:
    validate_node_id(approved_by_node_id)
    verified = verify_join_request(request, cluster_id=cluster_config.id)
    issued = _load_issued_request_invite(config, cluster_config.id, verified["invite_id"])
    secret = load_cluster_secret(config, cluster_config.id)
    keypair = load_node_keypair(config, cluster_config)
    encrypted = _encrypt_join_secret(
        issued["request_secret_b64"],
        verified["request_id"],
        {
            "cluster_id": secret.cluster_id,
            "data_keys": secret.data_keys,
            "active_key_id": secret.active_key_id,
            "active_key_hlc": secret.active_key_hlc,
        },
    )
    body = {
        "version": 1,
        "status": "approved",
        "cluster_id": cluster_config.id,
        "request_id": verified["request_id"],
        "approved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "approved_by_node_id": approved_by_node_id,
        "approved_node_id": verified["node"]["node_id"],
        "membership_record_id": membership_record_id,
        "encrypted_cluster_secret": encrypted,
    }
    signature = sign_ed25519(keypair.signing_private_key_b64, canonical_json_bytes(body))
    return {**body, "signature": signature}


def create_join_rejection(
    config: Config,
    cluster_config: ClusterConfig,
    *,
    request: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    verified = verify_join_request(request, cluster_id=cluster_config.id)
    keypair = load_node_keypair(config, cluster_config)
    body = {
        "version": 1,
        "status": "rejected",
        "cluster_id": cluster_config.id,
        "request_id": verified["request_id"],
        "rejected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rejected_by_node_id": cluster_config.node_id,
        "rejected_node_id": verified["node"]["node_id"],
        "reason": reason,
    }
    signature = sign_ed25519(keypair.signing_private_key_b64, canonical_json_bytes(body))
    return {**body, "signature": signature}


def apply_join_approval(config: Config, approval: dict[str, Any]) -> str:
    state = load_pending_join_state(config, approval.get("cluster_id"))
    if state is None:
        raise ValueError("No pending join state found")
    request = state["join_request"]
    invite = state["invite"]
    issuer_key = invite["body"]["issuer_signing_public_key_b64"]
    signature = approval.get("signature")
    body = dict(approval)
    body.pop("signature", None)
    if not verify_ed25519(issuer_key, canonical_json_bytes(body), signature):
        raise ValueError("Join approval signature is invalid")
    if approval.get("request_id") != state.get("request_id"):
        raise ValueError("Join approval request_id mismatch")
    if approval.get("status") == "rejected":
        state["status"] = "rejected"
        state["reason"] = approval.get("reason", "")
        _write_pending_join_state(config, approval["cluster_id"], state)
        return "rejected"
    if approval.get("status") != "approved":
        raise ValueError("Unsupported join approval status")
    if approval.get("approved_node_id") != request["node"]["node_id"]:
        raise ValueError("Join approval node_id mismatch")
    request_secret_b64 = state.get("invite_secret_b64")
    if not isinstance(request_secret_b64, str):
        raise ValueError("Pending join state is missing request secret")
    secret_payload = _decrypt_join_secret(
        request_secret_b64,
        approval["request_id"],
        approval["encrypted_cluster_secret"],
    )
    secret = ClusterSecret(
        cluster_id=secret_payload["cluster_id"],
        data_keys=dict(secret_payload["data_keys"]),
        active_key_id=secret_payload["active_key_id"],
        active_key_hlc=secret_payload.get("active_key_hlc"),
    )
    write_cluster_secret(config, secret)
    state["status"] = "approved"
    state["membership_record_id"] = approval.get("membership_record_id")
    _write_pending_join_state(config, approval["cluster_id"], state)
    return "approved"


def add_cluster_data_key(
    config: Config,
    cluster_id: str,
    key_id: str,
    data_key_b64: str,
    *,
    activate: bool,
    active_key_hlc: str | None = None,
) -> ClusterSecret:
    validate_cluster_id(cluster_id)
    validate_key_id(key_id)
    secret = load_cluster_secret(config, cluster_id)
    data_keys = dict(secret.data_keys)
    data_keys[key_id] = data_key_b64
    # HLC strings are fixed-width, so lexicographic order matches timestamp order.
    should_activate = activate and (
        secret.active_key_hlc is None or active_key_hlc is None or active_key_hlc >= secret.active_key_hlc
    )
    updated = ClusterSecret(
        cluster_id=cluster_id,
        data_keys=data_keys,
        active_key_id=key_id if should_activate else secret.active_key_id,
        active_key_hlc=active_key_hlc if should_activate else secret.active_key_hlc,
    )
    write_cluster_secret(config, updated)
    return updated


def _cluster_key_dir(config: Config, cluster_id: str) -> Path:
    validate_cluster_id(cluster_id)
    return config.cluster_keys_dir / cluster_id


def _secure_cluster_key_dir(config: Config, cluster_id: str) -> Path:
    config.cluster_keys_dir.mkdir(parents=True, exist_ok=True)
    harden_private_path(config.cluster_keys_dir, directory=True)
    key_dir = _cluster_key_dir(config, cluster_id)
    key_dir.mkdir(parents=True, exist_ok=True)
    harden_private_path(key_dir, directory=True)
    return key_dir


def _write_inviter_public_metadata(config: Config, cluster_id: str, invite_body: dict[str, Any]) -> None:
    validate_cluster_id(cluster_id)
    validate_node_id(invite_body["issuer_node_id"])
    path = config.clusters_dir / cluster_id / "nodes" / f"{invite_body['issuer_node_id']}.json"
    data = {
        "version": 1,
        "node_id": invite_body["issuer_node_id"],
        "alias": invite_body["issuer_alias"],
        "signing_public_key_b64": invite_body["issuer_signing_public_key_b64"],
        "encryption_public_key_b64": invite_body.get("issuer_encryption_public_key_b64"),
        "revoked": False,
        "revoked_after_hlc": None,
    }
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _verify_invite_payload(invite: InviteToken) -> None:
    body = invite.body
    if not verify_ed25519(body["issuer_signing_public_key_b64"], canonical_json_bytes(body), invite.signature):
        raise ValueError("Invite token signature is invalid")
    expires_at = _parse_iso_z(body["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise ValueError("Invite token has expired")


def _write_issued_request_invite(config: Config, cluster_id: str, invite_id: str, payload: dict[str, Any]) -> None:
    validate_cluster_id(cluster_id)
    validate_invite_id(invite_id)
    path = config.clusters_dir / cluster_id / "issued-invites" / f"{invite_id}.json"
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)


def _load_issued_request_invite(config: Config, cluster_id: str, invite_id: str) -> dict[str, Any]:
    validate_cluster_id(cluster_id)
    validate_invite_id(invite_id)
    path = config.clusters_dir / cluster_id / "issued-invites" / f"{invite_id}.json"
    if not path.exists():
        raise ValueError(f"No local request invite secret found for {invite_id}")
    data = json.loads(path.read_text())
    expires_at = _parse_iso_z(data["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise ValueError("Invite has expired")
    return data


def _write_pending_join_state(config: Config, cluster_id: str, payload: dict[str, Any]) -> None:
    path = _pending_join_state_path(config, cluster_id)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)


def _pending_join_state_path(config: Config, cluster_id: str) -> Path:
    validate_cluster_id(cluster_id)
    return config.clusters_dir / cluster_id / "join-state.json"


def _encrypt_join_secret(request_secret_b64: str, request_id: str, payload: dict[str, Any]) -> dict[str, str]:
    validate_join_request_id(request_id)
    encrypted = encrypt_payload(
        request_secret_b64,
        canonical_json_bytes(payload),
        f"om-join-approval:{request_id}".encode("utf-8"),
        key_id="key_join_approval",
    )
    return {
        "alg": encrypted.alg,
        "nonce": encrypted.nonce,
        "key_id": encrypted.key_id,
        "aad_hash": encrypted.aad_hash,
        "ciphertext": encrypted.ciphertext,
    }


def _decrypt_join_secret(request_secret_b64: str, request_id: str, encrypted: dict[str, str]) -> dict[str, Any]:
    validate_join_request_id(request_id)
    plaintext = decrypt_payload(
        request_secret_b64,
        EncryptedPayload(
            alg=encrypted["alg"],
            nonce=encrypted["nonce"],
            key_id=encrypted["key_id"],
            aad_hash=encrypted["aad_hash"],
            ciphertext=encrypted["ciphertext"],
        ),
        f"om-join-approval:{request_id}".encode("utf-8"),
    )
    return json.loads(plaintext.decode("utf-8"))


def _feature_env_signature() -> tuple[tuple[str, str | None], ...]:
    names = (
        "OM_CLUSTER_ENABLED",
        "OM_CLUSTER_ID",
        "OM_CLUSTER_DEFAULT_NAMESPACE",
        "OM_CLUSTER_SYNC_ON_OBSERVE",
        "OM_CLUSTER_SYNC_ON_REFLECT",
        "OM_CLUSTER_SYNC_BEFORE_CONTEXT",
        "OM_CLUSTER_STARTUP_PULL_DEADLINE_MS",
    )
    return tuple((name, os.environ.get(name)) for name in names)


def _path_signature(path: Path) -> tuple[bool, int | None, int | None]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (False, None, None)
    return (True, stat.st_mtime_ns, stat.st_size)


def _cached_key_signatures(cached: dict[str, Any]) -> tuple[tuple[str, tuple[bool, int | None, int | None]], ...]:
    return tuple((path, _path_signature(Path(path))) for path in cached.get("key_paths", ()))


def _store_feature_cache(
    cache_key: tuple[str, str, str],
    env_signature: tuple[tuple[str, str | None], ...],
    config_signature: tuple[bool, int | None, int | None],
    key_paths: list[Path],
    enabled: bool,
) -> None:
    _FEATURE_CACHE[cache_key] = {
        "env_signature": env_signature,
        "config_signature": config_signature,
        "key_paths": tuple(str(path) for path in key_paths),
        "key_signatures": tuple((str(path), _path_signature(path)) for path in key_paths),
        "enabled": enabled,
    }


def _env_or_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_duration(value: str) -> timedelta:
    value = value.strip().lower()
    if value.endswith("m"):
        return timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=int(value[:-1]))
    if value.endswith("d"):
        return timedelta(days=int(value[:-1]))
    return timedelta(seconds=int(value))


def _parse_iso_z(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
