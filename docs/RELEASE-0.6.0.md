# Release 0.6.0

This branch is prepared as the `observational-memory` `0.6.0` release candidate.

## What's In 0.6.0

- Adds OM Cluster, an opt-in sync layer for sharing memory across machines.
- Stores memory as signed, encrypted, append-only records before transport.
- Implements filesystem transport for Syncthing, Dropbox, iCloud Drive, NAS mounts, rsync, USB drives, and other shared directories.
- Adds an optional stdlib HTTP relay client for untrusted relay services that move opaque records and metadata without receiving plaintext or private keys.
- Adds optional explicit-peer P2P transport over the same opaque HTTP artifact contract, with no base dependency changes.
- Materializes `observations.md`, `reflections.md`, `profile.md`, and `active.md` locally from records and snapshots.
- Adds `om cluster init`, `invite`, `join`, `sync`, `status`, `peers`, `materialize`, `provenance`, `redact`, `revoke`, `rotate-key`, and `override`.
- Adds request-mode join approval commands: `om cluster requests`, `approve`, and `reject`. Trusted-direct invites remain available with `--mode trusted-direct`.
- Preserves existing non-cluster behavior unless cluster config and keys are explicitly initialized and enabled.
- Frames `rotate-key` honestly as key-epoch hygiene for trusted nodes. Revoked nodes are excluded from new wrapped data keys, and `om cluster reencrypt` can append new-key rewrap records for historical payloads.

## Validation

Local validation:

```bash
uv run pytest -q
uv run ruff check
```

Two-install sync smoke test:

```bash
om cluster init --name "Personal Memory" --transport filesystem:/tmp/om-cluster --import-existing
om cluster invite --expires 10m
# On a second install:
om cluster join "omc1:..."
# Back on a trusted install:
om cluster requests
om cluster approve join_...
# On the second install:
om cluster sync
```

The shared transport must contain encrypted `.omr.json` records only, never provider env files, private keys, `.cursor.json`, `.search-index`, `.scheduler-logs`, or generated Markdown as the sync source.

`om cluster rotate-key` in 0.6.0 creates a per-node wrapped key epoch for future records. `om cluster reencrypt` can append new-key `payload_rewrap` records for old payloads, but it does not automatically delete old transport blobs and cannot by itself protect old ciphertext from a device that already had the previous cluster key. For a known compromise, revoke the node, rotate for future writes, re-encrypt historical payloads, then inspect and clean transport/backups before treating old ciphertext as recovered.

## Publish To PyPI

After validation succeeds and the PR is merged:

```bash
make publish
```

## Tag And Trigger Homebrew

After the PyPI publish succeeds, tag the same release commit and push the tag:

```bash
git tag v0.6.0
git push origin main
git push origin v0.6.0
```

Do not push the tag before PyPI has `0.6.0`, or the Homebrew workflow will fail.

## Suggested Release Notes

```markdown
## Highlights

- OM Cluster syncs memory across machines through signed, encrypted append-only records.
- Filesystem, relay, and explicit-peer P2P transports move opaque signed/encrypted artifacts while treating the transport as untrusted.
- Markdown files remain local materialized views, so existing inspectable OM workflows stay intact.
- Cluster mode is opt-in and disabled unless initialized with `om cluster init` or `om cluster join`.
- Request-mode joins require approval from an already trusted node before the joining node receives encrypted cluster key material.
- Key rotation uses per-node wrapped key epochs, and `om cluster reencrypt` can append historical payload rewrap records after a rotation.
- Historical rewrap does not delete old transport blobs or backups automatically; compromise recovery still requires operator cleanup.

## Upgrade

Homebrew:

```bash
brew update
brew upgrade observational-memory
```

PyPI / uv tool:

```bash
uv tool install --reinstall observational-memory==0.6.0
om install
```

## Multi-Machine Sync

```bash
om cluster init --name "Personal Memory" --transport filesystem:~/Sync/om-cluster --import-existing
om cluster invite --expires 10m
om cluster requests
om cluster approve join_...
om cluster sync
```
```
