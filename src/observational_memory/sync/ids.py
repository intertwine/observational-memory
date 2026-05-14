"""Identifier validation for OM Cluster filesystem-facing names."""

from __future__ import annotations

import re

_CLUSTER_ID_RE = re.compile(r"^omc_[A-Za-z0-9_-]{16,}$")
_NODE_ID_RE = re.compile(r"^node_[A-Za-z0-9_-]{3,}$")
_RECORD_ID_RE = re.compile(r"^sha256_[a-f0-9]{64}$")
_KEY_ID_RE = re.compile(r"^key_[A-Za-z0-9_-]+$")
_INVITE_ID_RE = re.compile(r"^invite_[A-Za-z0-9_-]+$")
_JOIN_REQUEST_ID_RE = re.compile(r"^join_[A-Za-z0-9_-]+$")


def validate_cluster_id(value: str) -> str:
    return _validate(value, _CLUSTER_ID_RE, "cluster_id")


def validate_node_id(value: str) -> str:
    return _validate(value, _NODE_ID_RE, "node_id")


def validate_record_id(value: str) -> str:
    return _validate(value, _RECORD_ID_RE, "record_id")


def validate_key_id(value: str) -> str:
    return _validate(value, _KEY_ID_RE, "key_id")


def validate_invite_id(value: str) -> str:
    return _validate(value, _INVITE_ID_RE, "invite_id")


def validate_join_request_id(value: str) -> str:
    return _validate(value, _JOIN_REQUEST_ID_RE, "join_request_id")


def _validate(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid {label} {value!r}")
    return value
