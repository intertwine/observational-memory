"""Host-local OM Mail state: account, pinned peers, sync cursor, held messages.

Everything lives under ``<memory_dir>/mail/`` with 0600 file modes and is
never synced (same rule as ``usage.sqlite`` and ``.provider-jobs/``). Peer
trust is explicit: a peer's signing key is pinned at ``om mail peers add``
time, and shared pack keys are exchanged out of band.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from observational_memory.sync.atomic import atomic_write_text
from observational_memory.sync.crypto import b64url_encode, generate_node_keypair

if TYPE_CHECKING:
    from observational_memory.config import Config

    from .envelope import MailEnvelope

_SEEN_IDS_MAX = 2000


class MailAccountError(RuntimeError):
    """Mail account state is missing or invalid."""


@dataclass(frozen=True)
class MailAccount:
    provider: str
    inbox_id: str
    address: str
    display_name: str | None
    signing_private_key_b64: str
    signing_public_key_b64: str
    created_at: str


@dataclass(frozen=True)
class MailPeer:
    address: str
    alias: str | None
    signing_public_key_b64: str
    shared_key_b64: str | None = None
    allow_recall: bool = False
    auto_accept: bool = False


@dataclass
class MailState:
    cursor: str | None = None
    seen_ids: list[str] = field(default_factory=list)


def mail_dir(config: Config) -> Path:
    return config.memory_dir / "mail"


def account_path(config: Config) -> Path:
    return mail_dir(config) / "account.toml"


def peers_path(config: Config) -> Path:
    return mail_dir(config) / "peers.toml"


def state_path(config: Config) -> Path:
    return mail_dir(config) / "state.json"


def held_dir(config: Config) -> Path:
    return mail_dir(config) / "held"


def packs_dir(config: Config) -> Path:
    return mail_dir(config) / "packs"


def new_shared_key_b64() -> str:
    """Mint a symmetric pack key to be exchanged out of band (never over email)."""
    return b64url_encode(ChaCha20Poly1305.generate_key())


def new_mail_keypair() -> tuple[str, str]:
    """Return (signing_private_key_b64, signing_public_key_b64) for this account."""
    keypair = generate_node_keypair()
    return keypair.signing_private_key_b64, keypair.signing_public_key_b64


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def write_mail_account(config: Config, account: MailAccount) -> None:
    lines = [
        "[account]",
        f'provider = "{_toml_escape(account.provider)}"',
        f'inbox_id = "{_toml_escape(account.inbox_id)}"',
        f'address = "{_toml_escape(account.address)}"',
    ]
    if account.display_name:
        lines.append(f'display_name = "{_toml_escape(account.display_name)}"')
    lines.extend(
        [
            f'created_at = "{_toml_escape(account.created_at)}"',
            "",
            "[keys]",
            f'signing_private_key_b64 = "{_toml_escape(account.signing_private_key_b64)}"',
            f'signing_public_key_b64 = "{_toml_escape(account.signing_public_key_b64)}"',
            "",
        ]
    )
    mail_dir(config).mkdir(parents=True, exist_ok=True)
    atomic_write_text(account_path(config), "\n".join(lines).rstrip() + "\n", mode=0o600)


def load_mail_account(config: Config) -> MailAccount | None:
    path = account_path(config)
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
        account = data.get("account", {})
        keys = data.get("keys", {})
        return MailAccount(
            provider=str(account["provider"]),
            inbox_id=str(account["inbox_id"]),
            address=str(account["address"]),
            display_name=account.get("display_name"),
            signing_private_key_b64=str(keys["signing_private_key_b64"]),
            signing_public_key_b64=str(keys["signing_public_key_b64"]),
            created_at=str(account.get("created_at", "")),
        )
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise MailAccountError(f"Invalid mail account file: {path} ({exc})") from exc


def require_mail_account(config: Config) -> MailAccount:
    account = load_mail_account(config)
    if account is None:
        raise MailAccountError("No mail account configured. Run `om mail init` first.")
    return account


def write_mail_peers(config: Config, peers: dict[str, MailPeer]) -> None:
    lines: list[str] = []
    for peer in sorted(peers.values(), key=lambda p: p.address):
        lines.append("[[peer]]")
        lines.append(f'address = "{_toml_escape(peer.address)}"')
        if peer.alias:
            lines.append(f'alias = "{_toml_escape(peer.alias)}"')
        lines.append(f'signing_public_key_b64 = "{_toml_escape(peer.signing_public_key_b64)}"')
        if peer.shared_key_b64:
            lines.append(f'shared_key_b64 = "{_toml_escape(peer.shared_key_b64)}"')
        lines.append(f"allow_recall = {_toml_bool(peer.allow_recall)}")
        lines.append(f"auto_accept = {_toml_bool(peer.auto_accept)}")
        lines.append("")
    mail_dir(config).mkdir(parents=True, exist_ok=True)
    atomic_write_text(peers_path(config), "\n".join(lines).rstrip() + "\n" if lines else "", mode=0o600)


def load_mail_peers(config: Config) -> dict[str, MailPeer]:
    path = peers_path(config)
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise MailAccountError(f"Invalid mail peers file: {path} ({exc})") from exc
    peers: dict[str, MailPeer] = {}
    for entry in data.get("peer", []):
        try:
            peer = MailPeer(
                address=str(entry["address"]).strip().lower(),
                alias=entry.get("alias"),
                signing_public_key_b64=str(entry["signing_public_key_b64"]),
                shared_key_b64=entry.get("shared_key_b64"),
                allow_recall=bool(entry.get("allow_recall", False)),
                auto_accept=bool(entry.get("auto_accept", False)),
            )
        except (KeyError, TypeError) as exc:
            raise MailAccountError(f"Invalid peer entry in {path}: {exc}") from exc
        peers[peer.address] = peer
    return peers


def find_peer(config: Config, address: str) -> MailPeer | None:
    return load_mail_peers(config).get(address.strip().lower())


def upsert_peer(config: Config, peer: MailPeer) -> None:
    peers = load_mail_peers(config)
    peers[peer.address] = replace(peer, address=peer.address.strip().lower())
    write_mail_peers(config, peers)


def remove_peer(config: Config, address: str) -> bool:
    peers = load_mail_peers(config)
    if peers.pop(address.strip().lower(), None) is None:
        return False
    write_mail_peers(config, peers)
    return True


def load_mail_state(config: Config) -> MailState:
    path = state_path(config)
    if not path.exists():
        return MailState()
    try:
        data = json.loads(path.read_text())
        return MailState(
            cursor=data.get("cursor"),
            seen_ids=[str(item) for item in data.get("seen_ids", [])],
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        # A corrupt cursor only costs a re-fetch; seen-id dedup makes that safe.
        return MailState()


def write_mail_state(config: Config, state: MailState) -> None:
    mail_dir(config).mkdir(parents=True, exist_ok=True)
    payload = {
        "cursor": state.cursor,
        "seen_ids": state.seen_ids[-_SEEN_IDS_MAX:],
    }
    atomic_write_text(state_path(config), json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)


def hold_message(
    config: Config,
    *,
    message_id: str,
    sender: str,
    subject: str,
    reason: str,
    envelope: MailEnvelope | None = None,
    raw: bytes | None = None,
) -> Path:
    """Quarantine an inbound message that failed a trust check (fail closed)."""
    held_dir(config).mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "reason": reason,
        "held_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if envelope is not None:
        record["envelope"] = envelope.data
    elif raw is not None:
        record["raw_b64"] = b64url_encode(raw)
    path = held_dir(config) / f"{_safe_filename(message_id)}.json"
    atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n", mode=0o600)
    return path


def list_held(config: Config) -> list[dict[str, Any]]:
    directory = held_dir(config)
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            records.append({"message_id": path.stem, "reason": "unreadable held record"})
    return records


def load_held(config: Config, message_id: str) -> dict[str, Any] | None:
    path = held_dir(config) / f"{_safe_filename(message_id)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def remove_held(config: Config, message_id: str) -> bool:
    path = held_dir(config) / f"{_safe_filename(message_id)}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def _safe_filename(message_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in message_id)[:120]
