# OM Cluster Roadmap Status

This file tracks the long-running OM Cluster 0.6.x roadmap implementation across PR #33 and issues #34-#41.

## Milestone 0 - 0.6.0 Preview Hardening

Goals:

- Preserve the PR #33 filesystem-backed, opt-in preview baseline.
- Harden filesystem-facing cluster identifiers before path construction or glob use.
- Prevent older out-of-order imports from regressing per-node heads.
- Compute reflection catch-up from observation records only.
- Clarify that 0.6.0 key rotation is forward-looking key hygiene, not full compromise recovery.

Completed work:

- Added shared allow-list validators for cluster IDs, node IDs, record IDs, key IDs, invite IDs, and future join request IDs.
- Applied validation to config loading, key paths, record verification, record storage, public metadata storage, and filesystem transport path operations.
- Enforced head filename/body agreement, public-node filename/body agreement, and filesystem record path/body agreement.
- Replaced imported-head updates with monotonic head semantics so a later import of an older record stores the record without replacing the newer head record ID.
- Changed reflection snapshot catch-up detection to compare selected snapshot frontiers against `frontier_from_records(store.list_records(kind="observation"))`.
- Updated 0.6.0 release and OM Cluster docs with the key-rotation compromise-recovery caveat.

Tests added:

- Malformed record IDs are rejected before filesystem path use.
- Out-of-order imports do not regress head record IDs.
- Reflection catch-up ignores non-observation records when computing observation coverage.
- Filesystem transport rejects node metadata and record blobs whose filename/path metadata disagrees with the JSON body.

Validation:

- `mise exec -- uv run pytest tests/sync/test_store_and_materialize.py tests/sync/test_filesystem_sync.py -q` - 15 passed.
- `mise exec -- uv run pytest -q` - 339 passed.
- `mise exec -- uv run ruff check` - passed.

Known limitations:

- 0.6.0 still uses trusted direct invites by default; interactive approval is Milestone 1.
- 0.6.0 rotation does not perform per-node key epochs, historical rewrap, or purge.
- Windows cluster-specific ACL and path hardening remains Milestone 2.

Next milestone:

- Milestone 1: interactive approval for invite/join (#34).
