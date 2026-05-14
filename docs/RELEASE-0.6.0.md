# Release 0.6.0

This branch is prepared as the `observational-memory` `0.6.0` release candidate.

## What's In 0.6.0

- Adds OM Cluster, an opt-in sync layer for sharing memory across machines.
- Stores memory as signed, encrypted, append-only records before transport.
- Implements filesystem transport for Syncthing, Dropbox, iCloud Drive, NAS mounts, rsync, USB drives, and other shared directories.
- Materializes `observations.md`, `reflections.md`, `profile.md`, and `active.md` locally from records and snapshots.
- Adds `om cluster init`, `invite`, `join`, `sync`, `status`, `peers`, `materialize`, `provenance`, `redact`, `revoke`, `rotate-key`, and `override`.
- Preserves existing non-cluster behavior unless cluster config and keys are explicitly initialized and enabled.
- Frames `rotate-key` honestly as forward-looking key hygiene for trusted nodes. Full compromise recovery, per-node key epochs, and historical rewrap/purge semantics are planned after the preview.

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
om cluster sync
```

The shared transport must contain encrypted `.omr.json` records only, never provider env files, private keys, `.cursor.json`, `.search-index`, `.scheduler-logs`, or generated Markdown as the sync source.

`om cluster rotate-key` in 0.6.0 does not re-encrypt historical transport blobs and cannot by itself protect old records from a device that already had the previous cluster key. For a known compromise, revoke the node, rotate for future writes, inspect transport/backups, and wait for the key-epoch recovery work before treating old ciphertext as recovered.

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
- Filesystem transport works with common shared-folder tools while treating the transport as untrusted.
- Markdown files remain local materialized views, so existing inspectable OM workflows stay intact.
- Cluster mode is opt-in and disabled unless initialized with `om cluster init` or `om cluster join`.
- Key rotation in this preview is forward-looking; full compromise recovery and historical re-encryption are not included yet.

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
om cluster sync
```
```
