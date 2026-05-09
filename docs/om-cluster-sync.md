# OM Cluster Sync

OM Cluster syncs memory across machines by replicating encrypted, signed, append-only records. The human-readable Markdown files stay local materialized views.

## Quick Start

First machine:

```bash
om cluster init --name "Personal Memory" --transport filesystem:~/Sync/om-cluster --import-existing
om cluster invite --expires 10m
```

Second machine:

```bash
om cluster join "omc1:..."
om cluster sync
om cluster status
```

Use the transport directory with Syncthing, Dropbox, iCloud Drive, rsync, a NAS, or a mounted folder. Do not sync `~/.local/share/observational-memory/` directly.

## What Syncs

OM Cluster syncs:

- `observation` records
- `reflection_snapshot` records
- `manual_override` records
- `tombstone` redaction records
- `node_membership` records
- `key_rotation` records

OM Cluster materializes these local files from records:

- `observations.md`
- `reflections.md`
- `profile.md`
- `active.md`

OM Cluster never syncs:

- `~/.config/observational-memory/env`
- `~/.config/observational-memory/cluster-keys/`
- `.cursor.json`
- `.search-index/`
- `.scheduler-logs/`
- `.qmd-docs/`
- provider credentials
- private keys

## Security Model

The filesystem transport is treated as untrusted. Record payloads are encrypted with ChaCha20-Poly1305 and signed with Ed25519 node keys before they leave the local machine. The clear envelope contains routing metadata such as cluster ID, record kind, namespace, node ID, sequence, HLC timestamp, parents, and privacy-preserving source hints.

Unknown nodes are rejected by default. `om cluster join` creates an invite-backed membership record; existing nodes accept that membership only when the invite was signed by a trusted node and has not expired.

Invite tokens are sensitive. The current filesystem v1 invite is a trusted direct invite and includes cluster key material so the second machine can decrypt existing records. Copy it only over a trusted channel and keep the expiration short.

Private keys and provider credentials are stored only under the local config directory. Cluster key directories are owner-only (`0700`) and key files are owner-only (`0600`).

OM Cluster v1 uses a personal-cluster trust model: any currently trusted node can add, revoke, redact, or rotate cluster state. Do not add machines you would not trust with those administrative actions.

## Operations

Useful commands:

```bash
om cluster status
om cluster peers
om cluster sync
om cluster materialize
om cluster provenance <record-id>
om cluster redact --record <record-id> --reason "user-redaction"
om cluster revoke <node-id>
om cluster rotate-key
om cluster override add --target profile --section communication_style --body "..."
```

`om observe`, `om reflect`, and `om context` keep the old direct Markdown behavior unless cluster mode is enabled by local cluster config and keys. Sync failures do not block local memory capture.

## Materialization

Do not edit cluster-generated `observations.md`, `reflections.md`, `profile.md`, or `active.md` as durable sync sources. Durable changes should be represented as new records, for example:

```bash
om cluster override add --target profile --section communication_style --body "Prefer direct tradeoffs."
om cluster redact --record sha256_...
```

Reflection conflicts are handled as snapshots with frontiers. Incomparable snapshots are not line-merged; the next cluster-aware reflector run can consolidate them into a new snapshot covering both frontiers.

## Redaction Caveat

Redaction creates a tombstone record. Materializers and local search ignore tombstoned records after sync and reindex. Tombstones prevent future use, but they cannot guarantee erasure from machines, backups, logs, or shared folders that already saw the data.

## Key Rotation And Revocation

`om cluster revoke <node-id>` marks a node as revoked for future records. `om cluster rotate-key` creates a key-rotation record encrypted to the previous cluster key and uses the new key for future records. When peers import key-rotation records, the rotation with the greatest HLC timestamp becomes the active key, so future writes converge on the latest known rotation.

For a known device compromise, revoke the node, rotate the key, and inspect the shared transport. The v1 rotation path is forward-looking; it does not re-encrypt historical records.

## Discovery And P2P

The core sync engine is transport-agnostic. Filesystem transport is implemented first. LAN discovery and direct P2P/relay transports are currently extension seams, not required dependencies. Discovery must never imply trust; future discovered peers will still need membership authorization.

## Recovery

To disable cluster mode without deleting data:

```bash
OM_CLUSTER_ENABLED=0 om context
```

Or edit `~/.config/observational-memory/cluster.toml`:

```toml
[cluster]
enabled = false
```

Existing materialized Markdown remains readable. `om cluster init --import-existing` backs up current Markdown files under `~/.local/share/observational-memory/backups/cluster-init-<timestamp>/` before materialization.
