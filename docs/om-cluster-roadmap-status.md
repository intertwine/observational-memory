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

## Milestone 1 - Interactive Approval For Invite/Join

Goals:

- Make the recommended invite path request/approval based rather than trusted direct by default.
- Keep trusted-direct invite compatibility for offline/bootstrap setups.
- Ensure unapproved requesters do not receive cluster data keys or write accepted records.
- Let trusted nodes inspect, approve, and reject pending requests through filesystem transport.

Completed work:

- Added request-mode invite tokens as the default `om cluster invite` behavior. Request-mode token bodies omit cluster `data_keys`; trusted-direct tokens remain available via `--mode trusted-direct`.
- Added local issued-invite secrets on the trusted issuing node and local pending join state on the requester.
- Added transport-visible `join-requests` and `join-approvals` areas to the filesystem transport. Both enforce filename/body ID agreement.
- Added `om cluster requests`, `om cluster approve <request-id>`, and `om cluster reject <request-id>`.
- Added pending join status output and pending approval completion in `om cluster sync`.
- Approval creates a trusted-node-signed membership record, encrypts current cluster key material to the requester's local approval secret, and publishes the encrypted approval without plaintext keys in transport.
- Existing trusted-direct invite clusters and tests remain compatible when `--mode trusted-direct` is explicit.

Tests added:

- CLI request invite/approval flow proves request-mode tokens omit `data_keys`, pending joiners cannot sync before approval, trusted nodes can list and approve requests, approved nodes complete sync, and shared transport does not contain `request_secret_b64` or plaintext `data_keys`.

Validation:

- `mise exec -- uv run pytest tests/sync -q` - 28 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync src/observational_memory/cli.py tests/sync` - passed.
- `mise exec -- uv run pytest -q` - 340 passed.
- `mise exec -- uv run ruff check` - passed.

Known limitations:

- Request-mode approval currently depends on the issuing trusted node retaining the local issued-invite secret. A different already trusted node can see the request but cannot approve that specific invite unless it has the issuer's local approval secret.
- Approval uses a per-invite symmetric approval secret rather than the future per-node encryption-public-key model planned under key epochs.

Next milestone:

- Milestone 2: Windows cluster hardening (#40) and feature-flag caching (#38).

## Milestone 2 - Windows Hardening And Feature-Flag Caching

Goals:

- Preserve the merged #39 Windows baseline while adding cluster-specific Windows path and key diagnostics.
- Add a small cache for cluster feature-gate checks without stale env/config/key behavior.

Completed work:

- Added `clear_cluster_feature_cache()` and a process-local cache for `cluster_feature_enabled(config)`.
- Cache reuse is gated by config path identity, config file existence/mtime/size, relevant `OM_CLUSTER_*` env overrides, and known node/cluster key file existence/mtime/size.
- Added tests proving unchanged calls reuse loaded key state, env override changes are observed in-process, and deleted key files invalidate the cache.
- Added platform-aware private-path permission helpers for OM Cluster.
- On POSIX, cluster key directories continue to be hardened/verified with `0700` and key files with `0600`.
- On Windows, `om doctor` reports an honest warning when portable ACL owner-only verification is unavailable instead of claiming POSIX mode safety.
- Added Windows CLI coverage for `%LOCALAPPDATA%` filesystem transport expansion and simulated Windows cluster doctor diagnostics.

Tests added:

- Cluster feature cache reuse and key-file invalidation tests.
- Windows `om cluster init` with `%LOCALAPPDATA%` transport path expansion.
- Windows `om doctor --json` warning for OM Cluster key ACL verification.

Validation:

- `mise exec -- uv run pytest tests/sync/test_config_feature_flag.py tests/test_windows.py -q` - 36 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync src/observational_memory/cli.py tests/sync/test_config_feature_flag.py tests/test_windows.py` - passed.
- `mise exec -- uv run pytest -q` - 344 passed.
- `mise exec -- uv run ruff check` - passed.

Known limitations:

- Real NTFS ACL introspection is not implemented yet; Windows diagnostics are deliberately conservative.
- A real Windows two-install filesystem smoke should still be attached before broad release messaging.

Next milestone:

- Milestone 3: reflection metadata, stale-state pruning, host-memory coexistence, and cluster-aware semantic merge (#41).

## Milestone 3 - Reflection Metadata, Stale-State Pruning, And Coexistence

Goals:

- Add per-entry metadata to generated reflections.
- Preserve legacy/manual reflection text while assigning stable IDs and kinds.
- Age out stale snapshot facts without pruning evergreen memory.
- Document host-agent memory coexistence and cluster semantic merge behavior.

Completed work:

- Added `reflection_metadata.py` for parsing, formatting, kind inference, metadata backfill, and stale snapshot pruning.
- Reflection generation now post-processes outputs with inline metadata comments containing `id`, `kind`, `last_seen`, `node`, and `scope`.
- Added `OM_SNAPSHOT_TTL_DAYS` and `OM_SNAPSHOT_EXPIRY_ACTION` config/env settings.
- Added `om prune` with `--dry-run`, `--json`, `--drop-stale`, and reserved `--namespace`.
- Added docs for host-memory coexistence and updated cluster docs with metadata-based merge rules.

Tests added:

- Metadata backfill preserves existing IDs and unknown fields.
- Stale snapshots move to `## Stale snapshots` idempotently while evergreen entries remain.
- `om prune --json` exercises the command path.

Validation:

- `mise exec -- uv run pytest tests/test_reflection_metadata.py tests/test_reflect.py -q` - 49 passed.
- `mise exec -- uv run ruff check src/observational_memory/reflection_metadata.py src/observational_memory/reflect.py src/observational_memory/cli.py tests/test_reflection_metadata.py` - passed.
- `mise exec -- uv run pytest -q` - 347 passed.
- `mise exec -- uv run ruff check` - passed.

Known limitations:

- Metadata kind inference is heuristic for legacy entries.
- `--namespace` is accepted for future cluster-scoped pruning but local Markdown pruning is not namespace-filtered yet.

Next milestone:

- Milestone 4: namespace/source policy, override semantics, and record indexing.
