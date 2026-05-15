# OM Cluster Public Validation Checklist

This checklist is safe to share in public logs. Use temporary directories, loopback relay URLs, and fake memory values. Do not include private hostnames, IPs, tunnels, provider keys, or real memory values.

Run normal quality checks first:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## 1. Baseline Without Cluster

```bash
OM_CLUSTER_ENABLED=0 om context
om recall --query "current work" --limit 3
```

Expected:

- `om context` returns a bounded startup payload with recall handles.
- `om recall` works without cluster mode.
- Existing local Markdown remains readable.

## 2. Request Approval And Convergence

Create at least two temp OM homes and one shared transport directory or loopback relay.

```bash
om cluster init --name "Temp Validation" --node-alias node-a --transport filesystem:/tmp/om-shared
om cluster invite --expires 1h
om cluster join <token> --node-alias node-b
om cluster requests --json
om cluster approve <request-id>
om cluster sync --json
om cluster status --json
```

Expected:

- Request-mode invites do not contain `data_keys`.
- The joining node is disabled until approval.
- After approval and sync, trusted peers appear in `peers`, not `pending_peers`.
- Status includes `transport_diagnostics`, `review_artifacts`, and any remediation text.

## 3. Relay Transport

Run a local relay:

```bash
om-relay --storage-dir /tmp/om-relay --host 127.0.0.1 --port 8765
om cluster relay health http://127.0.0.1:8765 --artifact-dir /tmp/om-relay --json
```

Expected:

- Health reports `ok: true`.
- Artifact scan reports no provider keys, node private keys, request secrets, plaintext memory values, or `data_keys`.
- Stopping the relay causes sync to report a transport error without deleting local records or breaking local materialization.

## 4. Redaction

Write a synthetic observation, sync it, redact the record, sync again, and reindex.

Expected:

- Tombstoned records are absent from generated Markdown and local search.
- The tombstone record itself is visible in cluster record counts.
- Redaction does not claim to erase backups or already-synced transport blobs.

## 5. Key Epochs And Rewrap

```bash
om cluster rotate-key
om cluster reencrypt --dry-run
om cluster reencrypt
om cluster purge-old-ciphertext --key-id <old-key-id>
```

Expected:

- New writes use the latest active key.
- Trusted peers import the key epoch.
- Revoked peers reject new key material.
- Rewrap records materialize old payloads when the old key is no longer present.
- Purge output remains a readiness report, not automatic deletion.

## 6. Reflection Metadata And Conflicts

Create two synthetic reflection snapshots with conflicting preference, policy, identity, decision, mode, or high-actionability entries.

Expected:

- Metadata preserves unknown fields and includes actionability/sensitivity/source fields on generated entries.
- `scope=local` entries do not become shared cluster reflection payload.
- Conflict artifacts appear under `clusters/<cluster-id>/review/reflection-conflicts.{json,md}`.
- `om cluster status --json` reports the review artifact count.

## 7. Public Evidence

Safe evidence to commit or paste:

- Command names and synthetic temp paths.
- JSON keys, counts, and pass/fail summaries.
- Redacted status output that excludes real hostnames/IPs and secrets.

Do not commit:

- Private relay URLs, tunnels, hostnames, IPs, usernames, or filesystem paths.
- Provider API keys or `.env` files.
- Real memory values from a personal cluster.
- Relay artifact dumps from a private cluster.
