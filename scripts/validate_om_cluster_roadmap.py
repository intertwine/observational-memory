"""End-to-end OM Cluster roadmap smoke validation."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.config import Config
from observational_memory.sync.config import load_cluster_secret, write_cluster_secret
from observational_memory.sync.crypto import ClusterSecret
from observational_memory.sync.engine import sync_cluster
from observational_memory.sync.materialize import materialize_cluster_memory
from observational_memory.sync.store import ClusterStore


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="om-cluster-roadmap-") as raw:
        root = Path(raw)
        shared = root / "shared"
        env_a = _env(root, "a")
        env_b = _env(root, "b")
        runner = CliRunner()

        _run(
            runner,
            ["cluster", "init", "--name", "Roadmap", "--node-alias", "node-a", "--transport", f"filesystem:{shared}"],
            env_a,
        )
        invite = _run(runner, ["cluster", "invite", "--expires", "1h"], env_a).stdout.strip()
        join = _run(runner, ["cluster", "join", invite, "--node-alias", "node-b"], env_b)
        request_id = [part for part in join.output.split() if part.startswith("join_")][0]
        pending = runner.invoke(cli, ["cluster", "sync"], env=env_b)
        assert pending.exit_code != 0, "pending join unexpectedly synced before approval"
        _run(runner, ["cluster", "approve", request_id], env_a)
        _run(runner, ["cluster", "sync", "--json"], env_b)

        config_a = _config(root, "a")
        config_b = _config(root, "b")
        store_a = ClusterStore.from_config(config_a)
        store_b = ClusterStore.from_config(config_b)
        observation_a = store_a.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "roadmap", "host_alias": "node-a"},
            payload={"format": "markdown", "body": "- roadmap memory from A", "observed_at": "2026-05-14T12:00:00Z"},
        )
        store_b.append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "roadmap", "host_alias": "node-b"},
            payload={"format": "markdown", "body": "- roadmap memory from B", "observed_at": "2026-05-14T12:01:00Z"},
        )
        _sync_pair(config_a, config_b)
        materialize_cluster_memory(config_a, ClusterStore.from_config(config_a))
        materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))
        assert "roadmap memory from A" in config_b.observations_path.read_text()
        assert "roadmap memory from B" in config_a.observations_path.read_text()

        _run(runner, ["cluster", "redact", "--record", observation_a.record_id, "--reason", "roadmap-smoke"], env_a)
        _sync_pair(config_a, config_b)
        materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))
        assert "roadmap memory from A" not in config_b.observations_path.read_text()

        ClusterStore.from_config(config_a).append_record(
            kind="observation",
            namespace="personal",
            source={"agent": "roadmap", "host_alias": "node-a"},
            payload={"format": "markdown", "body": "- roadmap old-key memory", "observed_at": "2026-05-14T12:02:00Z"},
        )
        _sync_pair(config_a, config_b)
        _run(runner, ["cluster", "rotate-key"], env_a)
        _sync_pair(config_a, config_b)
        _run(runner, ["cluster", "reencrypt"], env_a)
        _sync_pair(config_a, config_b)
        _drop_inactive_keys(config_b)
        materialize_cluster_memory(config_b, ClusterStore.from_config(config_b))
        assert "roadmap old-key memory" in config_b.observations_path.read_text()

        shared_bytes = b"".join(path.read_bytes() for path in shared.glob("clusters/**/*") if path.is_file())
        for forbidden in [b"roadmap memory", b"roadmap old-key", b"request_secret_b64", b"data_keys"]:
            assert forbidden not in shared_bytes, f"transport leaked {forbidden!r}"

        print(
            json.dumps(
                {
                    "status": "ok",
                    "shared_path": str(shared),
                    "checks": [
                        "request approval",
                        "two-node convergence",
                        "redaction",
                        "key epoch",
                        "historical rewrap after old-key removal",
                        "transport secrecy",
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )


def _run(runner: CliRunner, args: list[str], env: dict[str, str]):
    with _patched_env(env):
        result = runner.invoke(cli, args, env=env)
    assert result.exit_code == 0, result.output
    return result


def _sync_pair(config_a: Config, config_b: Config) -> None:
    sync_cluster(config_a)
    sync_cluster(config_b)
    sync_cluster(config_a)
    sync_cluster(config_b)


def _drop_inactive_keys(config: Config) -> None:
    cluster_id = ClusterStore.from_config(config).cluster_config.id
    secret = load_cluster_secret(config, cluster_id)
    write_cluster_secret(
        config,
        ClusterSecret(
            cluster_id=secret.cluster_id,
            data_keys={secret.active_key_id: secret.data_keys[secret.active_key_id]},
            active_key_id=secret.active_key_id,
            active_key_hlc=secret.active_key_hlc,
        ),
    )


def _env(root: Path, name: str) -> dict[str, str]:
    return {
        "HOME": str(root / f"{name}-home"),
        "XDG_CONFIG_HOME": str(root / f"{name}-config"),
        "XDG_DATA_HOME": str(root / f"{name}-data"),
        "CODEX_HOME": str(root / f"{name}-codex"),
    }


def _config(root: Path, name: str) -> Config:
    return Config(
        memory_dir=root / f"{name}-data" / "observational-memory",
        env_file=root / f"{name}-config" / "observational-memory" / "env",
    )


@contextmanager
def _patched_env(env: dict[str, str]):
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()
