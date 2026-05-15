# OM Cluster Sync

OM Cluster syncs memory across machines by moving encrypted, signed, append-only records. The readable Markdown files stay local materialized views.

Cluster mode is opt-in. Nothing syncs until you run `om cluster init` or `om cluster join`.

## Quick Start

First machine:

```bash
om cluster init --name "Personal Memory" --transport filesystem:~/Sync/om-cluster --import-existing
om cluster invite --expires 10m
```

Second machine:

```bash
om cluster join "omc1:..."
om cluster status
```

Back on a trusted machine:

```bash
om cluster requests
om cluster approve join_...
```

Then on the second machine:

```bash
om cluster sync
om cluster status
```

Use the transport directory with Syncthing, Dropbox, iCloud Drive, rsync, a NAS, or a mounted folder. Do not sync `~/.local/share/observational-memory/` directly.

## What Syncs

OM Cluster syncs these record kinds:

- `observation` records
- `reflection_snapshot` records
- `manual_override` records
- `tombstone` redaction records
- `node_membership` records
- `key_epoch` records
- `payload_rewrap` records

OM Cluster rebuilds these local files from records:

- `observations.md`
- `reflections.md`
- `profile.md`
- `active.md`

OM Cluster does not sync:

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

Unknown nodes are rejected by default. The default `om cluster invite` mode creates a request-mode invite. `om cluster join` uses that token to publish a signed pending join request. An already trusted node must approve it with `om cluster approve <request-id>` before the new node receives encrypted cluster key material. Transport visibility does not imply trust.

Public metadata from unknown nodes may be cached as pending peers for diagnostics, but it does not authorize those nodes or their records. `om cluster status --json` reports pending peers so operators can inspect unexpected shared-folder activity.

Trusted direct invites remain available for offline/bootstrap setups:

```bash
om cluster invite --mode trusted-direct --expires 10m
```

Trusted direct invite tokens are sensitive because they include cluster key material so the second machine can decrypt existing records immediately. Copy them only over a trusted channel and keep the expiration short. Request-mode invite tokens do not contain cluster data keys, but they still contain a local approval secret for the requester and should be kept private until the request is approved or expires.

Private keys and provider credentials are stored only under the local config directory. Cluster key directories are owner-only (`0700`) and key files are owner-only (`0600`).

OM Cluster v1 uses a personal-cluster trust model. Any currently trusted node can add, revoke, redact, or rotate cluster state. Do not add machines you would not trust with those actions.

## Operations

Useful commands:

```bash
om cluster status
om cluster peers
om cluster requests
om cluster approve <request-id>
om cluster reject <request-id> --reason "..."
om cluster namespace list
om cluster source-policy list
om cluster sync
om cluster materialize
om cluster provenance <record-id>
om cluster redact --record <record-id> --reason "user-redaction"
om cluster revoke <node-id>
om cluster rotate-key
om cluster override add --target profile --section communication_style --body "..."
om cluster override set --target profile --section communication_style --body "..."
om cluster override get --target profile --section communication_style
```

`om observe`, `om reflect`, and `om context` keep the old direct Markdown behavior unless cluster mode is enabled by local cluster config and keys. Sync failures do not block local memory capture.

`om context` can pull before startup when cluster config enables it. The pull has a short deadline so startup does not hang on a bad transport.

## Materialization

Do not edit cluster-generated `observations.md`, `reflections.md`, `profile.md`, or `active.md` as durable sync sources. Durable changes should be represented as new records, for example:

```bash
om cluster override add --target profile --section communication_style --body "Prefer direct tradeoffs."
om cluster redact --record sha256_...
```

Reflection conflicts use snapshots, frontiers, and inline entry metadata. Snapshot entries use `last_seen` to prefer newer state. Entries marked `scope=local` are removed from shared cluster reflection snapshots and hidden when they arrive from another node.

Non-snapshot conflicts are surfaced as review artifacts instead of being silently smoothed away. If policy, preference, identity, decision, mode, or high-actionability entries disagree across reflection snapshots, materialization writes:

```text
~/.local/share/observational-memory/clusters/<cluster-id>/review/reflection-conflicts.json
~/.local/share/observational-memory/clusters/<cluster-id>/review/reflection-conflicts.md
```

`om cluster status --json` reports the conflict count under `review_artifacts.reflection_conflicts` and includes public-safe remediation text.

## Namespaces, Source Policies, And Overrides

Namespaces are operator-visible routing labels for records. Source policies route matching source metadata into namespaces without editing TOML manually:

```bash
om cluster namespace add project:observational-memory
om cluster source-policy add --agent codex --namespace project:observational-memory
om cluster source-policy add --path-contains /work/private --namespace local:private --local-only
```

Manual profile/active overrides are latest-wins by `(target, section, namespace)`. `set` creates a new override record, `get` resolves the latest value, and `remove --target ... --section ...` creates a removal record. Older records remain in the append-only log for provenance.

The local record index at `clusters/<cluster-id>/index/records.json` is rebuildable and non-authoritative. If it is missing or stale, OM falls back to record files and can rebuild the index from encrypted record envelopes.

## Redaction Caveat

Redaction creates a tombstone record. Materializers and local search ignore tombstoned records after sync and reindex. Tombstones prevent future use, but they cannot guarantee erasure from machines, backups, logs, or shared folders that already saw the data.

## Key Rotation And Revocation

`om cluster revoke <node-id>` marks a node as revoked for future records. `om cluster rotate-key` creates a key-epoch record encrypted to the previous cluster key. The new data key inside that record is separately wrapped to each currently trusted node's encryption public key, and revoked nodes are excluded. When peers import key-epoch records, the rotation with the greatest HLC timestamp becomes the active key, so future writes converge on the latest known rotation.

For a known device compromise, revoke the node, rotate the key, and inspect the shared transport. Key epochs prevent the revoked node from learning new keys, but they do not automatically rewrite old transport blobs or backups that were already encrypted to a key the device had. Full old-ciphertext recovery requires a deliberate historical rewrap or purge pass.

`om cluster reencrypt` appends `payload_rewrap` records for historical observation, reflection, and override payloads that are still encrypted under older data keys. Materializers prefer a valid latest rewrap while preserving the original record ID, node, HLC, namespace, and source as provenance. Use `om cluster reencrypt --dry-run` or `--from-key <key-id>` to inspect the scope before writing rewrap records.

`om cluster purge-old-ciphertext --key-id <key-id>` is a readiness report, not an automatic deletion command. True old-ciphertext recovery requires removing old encrypted blobs from every shared transport and backup that a revoked device could still access, so OM does not delete append-only records automatically.

## Relay, Discovery, And P2P

The sync engine does not trust the transport. Filesystem transport works for shared folders. Relay transport works with an HTTP service that stores opaque cluster artifacts:

```bash
om cluster init --name "Personal Memory" --transport relay:https://relay.example.com
```

The relay stores signed/encrypted records, heads, public node metadata, join requests, and join approvals only. It never receives cluster data keys, node private keys, provider env files, generated Markdown, or plaintext memory. Relay access control can prevent abuse, but relay access is not cluster trust; local nodes still verify membership, signatures, revocation, tombstones, key epochs, and payload hashes exactly as they do with filesystem transport.

The base install includes a supported stdlib relay server; no heavyweight dependency is required:

```bash
om-relay --storage-dir /var/lib/om-relay --host 127.0.0.1 --port 8765
om cluster relay serve --storage-dir /var/lib/om-relay --host 127.0.0.1 --port 8765
om cluster relay health http://127.0.0.1:8765 --artifact-dir /var/lib/om-relay --json
```

Relay operator responsibilities include retention, availability, metadata exposure, and backup cleanup during compromise recovery. The `relay health` command checks reachability, relay version/storage metadata, and scans relay artifacts for obvious plaintext secrets such as provider keys, node private keys, request secrets, or `data_keys`.

Example systemd unit:

```ini
[Unit]
Description=OM Cluster relay
After=network-online.target

[Service]
ExecStart=/usr/local/bin/om-relay --storage-dir /var/lib/om-relay --host 127.0.0.1 --port 8765
Restart=on-failure
User=om-relay
Group=om-relay

[Install]
WantedBy=multi-user.target
```

Example launchd program arguments:

```xml
<array>
  <string>/usr/local/bin/om-relay</string>
  <string>--storage-dir</string>
  <string>/usr/local/var/om-relay</string>
  <string>--host</string>
  <string>127.0.0.1</string>
  <string>--port</string>
  <string>8765</string>
</array>
```

Direct P2P uses explicit peer URLs:

```bash
om cluster init --name "Personal Memory" --transport p2p:http://peer.local:8765
om cluster p2p status
om cluster p2p peers
```

The first P2P implementation uses the same opaque HTTP artifact contract as relay and is meant for LAN, Tailscale, or operator-managed tunnels. It does not do automatic NAT traversal or discovery yet. Discovery must never imply trust; future discovered peers will still need membership authorization.

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

## Windows Notes

On Windows, OM uses `%APPDATA%\observational-memory\cluster.toml` for cluster config and `%APPDATA%\observational-memory\cluster-keys` for local key material, with cluster records under `%LOCALAPPDATA%\observational-memory\clusters` unless XDG paths are explicitly set.

Filesystem transport paths may use drive-letter paths or environment variables:

```powershell
om cluster init --transport filesystem:C:\Users\Bryan\Sync\om-cluster
om cluster init --transport filesystem:%LOCALAPPDATA%\OM\cluster
```

`om doctor` does not claim POSIX `0600`/`0700` bits prove NTFS owner-only ACLs. On Windows it reports a warning when portable ACL verification is not available, so users know to validate key directories on the actual machine.

Feature-gate checks for cluster mode are cached within a process, but the cache includes the cluster config file signature, local key file signatures, relevant `OM_CLUSTER_*` environment overrides, and config root paths. Local config or key changes are reflected without restarting normal CLI commands.
