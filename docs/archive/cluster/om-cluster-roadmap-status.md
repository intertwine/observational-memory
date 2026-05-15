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

## Milestone 4 - Namespace/Source Policy, Override Semantics, And Record Indexing

Goals:

- Add operator-facing namespace and source-policy commands.
- Make manual override materialization deterministic and latest-wins.
- Add a rebuildable record index for record ID lookup without making it authoritative.

Completed work:

- Added `om cluster namespace list/add/remove`.
- Added `om cluster source-policy list/add` with agent, git remote hash, path substring, namespace, and local-only fields.
- Extended `NamespaceRule` persistence with `local_only`.
- Added `om cluster override set/get/list --json/remove --target --section` while preserving record-ID removal.
- Changed materialization so manual overrides resolve latest-wins by `(namespace, section)`, and latest remove records suppress previous values.
- Added `ClusterStore.record_index_path`, incremental index updates on record writes, and `rebuild_record_index()`.
- `_record_path_by_id()` now consults the local index first and falls back to filesystem glob lookup when the index is absent or stale.

Tests added:

- Record index is written and rebuildable.
- Manual overrides resolve latest-wins and removal records suppress previous values.
- CLI namespace/source-policy/override semantics exercise the operator path.

Validation:

- `mise exec -- uv run pytest tests/sync/test_store_and_materialize.py tests/sync/test_filesystem_sync.py -q` - 19 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync src/observational_memory/cli.py tests/sync/test_store_and_materialize.py tests/sync/test_filesystem_sync.py` - passed.
- `mise exec -- uv run pytest -q` - 350 passed.
- `mise exec -- uv run ruff check` - passed.

Known limitations:

- Source-policy privacy modes are still basic; full hashed/minimal presentation policy remains a later polish layer.
- The record index does not replace encrypted record files as the source of truth and intentionally stores only clear envelope metadata.

Next milestone:

- Milestone 5: key epochs and historical rewrap (#37).

## Milestone 5 - Key Epochs And Revocation-Aware Rotation

Goals:

- Stop storing new data keys directly inside cluster-wide rotation records.
- Give each node a stable encryption keypair distinct from its signing keypair.
- Rotate by publishing a signed key-epoch record with the new data key wrapped separately to currently trusted nodes.
- Exclude revoked nodes from new epochs so old cluster key material cannot unlock future writes.
- Add an append-only historical rewrap workflow without deleting old ciphertext by default.

Completed work:

- Extended node keypairs with X25519 encryption keys and backfilled missing encryption keys for existing local node key files.
- Added X25519, HKDF-SHA256, and ChaCha20-Poly1305 key wrapping helpers.
- Published node encryption public keys in local node metadata, invite bodies, direct membership records, join requests, and approved membership records.
- Changed `om cluster rotate-key` to write `key_epoch` records whose recipients list contains one wrapped key per non-revoked node with an encryption public key.
- Added key-epoch import handling that unwraps only the local node recipient and activates the epoch by HLC order.
- Added `payload_rewrap` helpers and `om cluster reencrypt` so historical payloads can be copied into new-key ciphertext while preserving original record provenance.
- Materializers now prefer valid latest rewrap payloads for target records, allowing active nodes to materialize old memories after old local data keys are removed.
- Added `om cluster purge-old-ciphertext --key-id ...` as a guarded readiness report that warns operators to inspect shared transports and backups rather than deleting append-only records automatically.
- Preserved legacy `key_rotation` import handling for compatibility with older preview records.
- Fixed `ensure_layout()` so materialization and sync no longer revive a locally known revoked node.

Tests added:

- Key epochs propagate the active key to a trusted peer and future writes use the new active key.
- A revoked peer remains revoked locally, rejects a post-revocation key epoch that excludes it, and keeps its old active key.
- Historical rewrap lets an active peer materialize an old observation after its old data key is removed locally.
- A revoked peer rejects new-key rewrap records and does not activate the new key.

Validation:

- `mise exec -- uv run pytest tests/sync/test_filesystem_sync.py::test_key_rotation_propagates_active_key_to_peer tests/sync/test_filesystem_sync.py::test_key_epoch_excludes_revoked_peer_from_new_active_key -q` - 2 passed.
- `mise exec -- uv run pytest tests/sync/test_filesystem_sync.py::test_historical_rewrap_materializes_after_old_key_removed tests/sync/test_filesystem_sync.py::test_revoked_peer_cannot_import_new_key_rewrap -q` - 2 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync/crypto.py src/observational_memory/sync/config.py src/observational_memory/sync/store.py src/observational_memory/cli.py tests/sync/test_filesystem_sync.py` - passed.

Known limitations:

- Rewrap is append-only and does not delete old ciphertext; transport and backup cleanup remains an explicit operator recovery step.
- Nodes that never publish an encryption public key are excluded from new key epochs and must refresh metadata before receiving future keys.

Next milestone:

- Milestone 6: hosted relay transport (#35).

## Milestone 6 - Hosted Relay Transport

Goals:

- Add an optional relay transport that moves only opaque signed/encrypted cluster artifacts.
- Keep relay access separate from cluster trust and preserve all local verification semantics.
- Avoid adding required relay server or HTTP client dependencies to the base package.
- Prove two nodes can converge without a shared filesystem.

Completed work:

- Added a stdlib `RelayTransport` client implementing the existing sync transport protocol over HTTP.
- Supported relay storage for heads, public node metadata, records, join requests, and join approvals.
- Added `relay:http://...` and `relay:https://...` transport specs for `om cluster init`.
- Extended transport construction so relay can run alongside filesystem transport.
- Documented relay operator boundaries, metadata exposure, retention, and compromise-recovery caveats.

Tests added:

- Loopback relay fixture proves two trusted nodes converge through relay transport with no shared filesystem.
- Relay fixture storage is checked for plaintext memory, private signing key fields, and cluster `data_keys` leakage.

Validation:

- `mise exec -- uv run pytest tests/sync/test_relay_transport.py -q` - 1 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync/transports/relay.py src/observational_memory/sync/engine.py src/observational_memory/cli.py tests/sync/test_relay_transport.py` - passed.

Known limitations:

- This implements the relay client contract and test fixture, not a production relay server package.
- Relay authorization tokens and retention policy are operator concerns and do not replace OM Cluster membership.

Next milestone:

- Milestone 7: optional direct P2P evaluation and transport (#36).

## Milestone 7 - Optional Direct P2P Transport

Goals:

- Add a low-dependency direct peer transport after key epochs and relay are stable.
- Keep discovery and reachability separate from OM Cluster membership.
- Preserve base install behavior without P2P-only dependencies.
- Document the transport choice before implementation.

Completed work:

- Added `docs/om-cluster-p2p-evaluation.md` selecting explicit HTTP peer endpoints over heavyweight NAT traversal dependencies for the first P2P implementation.
- Added `P2PTransport`, an explicit-peer adapter that reuses the relay opaque artifact contract against one or more peer URLs.
- Added `p2p:http://...[,http://...]` transport parsing for `om cluster init`.
- Added `om cluster p2p status` and `om cluster p2p peers` inspection commands that state reachability is not trust.
- Documented LAN/Tailscale/operator-tunnel scope and current non-goals such as mDNS and NAT traversal.

Tests added:

- Two loopback peer endpoints converge without a shared filesystem or hosted relay, while peer endpoint storage does not contain plaintext memory.

Validation:

- `mise exec -- uv run pytest tests/sync/test_p2p_transport.py -q` - 1 passed.
- `mise exec -- uv run ruff check src/observational_memory/sync/transports/p2p.py src/observational_memory/sync/engine.py src/observational_memory/cli.py tests/sync/test_p2p_transport.py docs/om-cluster-p2p-evaluation.md` - passed.

Known limitations:

- No automatic discovery or NAT traversal.
- No packaged long-running peer server yet; the first implementation validates the direct transport protocol with loopback HTTP endpoints.

Next milestone:

- Milestone 8: final stabilization, dogfood validation, release packaging, and docs consolidation.

## Milestone 8 - Final Stabilization And Release Readiness

Goals:

- Consolidate user-facing docs so setup, transports, approval, key epochs, rewrap, redaction, and recovery match actual behavior.
- Add a repeatable dogfood validation path that exercises the highest-risk multi-node flows.
- Preserve full test/lint cleanliness before release packaging.

Completed work:

- Updated README sync guidance for request approval, filesystem, relay, P2P, key epochs, historical rewrap, and transport secrecy.
- Added `scripts/validate_om_cluster_roadmap.py`, an isolated two-node filesystem dogfood smoke that validates request approval, convergence, redaction, key epoch rotation, historical rewrap after old-key removal, and transport secrecy.
- Adjusted rewrap candidate handling so tombstone records can be rewrapped too; this prevents tombstoned content from reappearing after an operator removes inactive local data keys.

Tests added:

- The dogfood script runs against temporary HOME/XDG roots and a temporary filesystem transport.

Validation:

- `mise exec -- uv run python scripts/validate_om_cluster_roadmap.py` - passed.
- `mise exec -- uv run pytest tests/sync/test_filesystem_sync.py::test_historical_rewrap_materializes_after_old_key_removed -q` - 1 passed.
- `mise exec -- uv run ruff check scripts/validate_om_cluster_roadmap.py src/observational_memory/sync/store.py` - passed.
- `mise exec -- uv run pytest -q` - 355 passed.
- `mise exec -- uv run ruff check` - passed.
- `mise exec -- make build` - built `observational_memory-0.6.0.tar.gz` and `observational_memory-0.6.0-py3-none-any.whl`.
- `mise exec -- uv tool run --isolated --from ./dist/observational_memory-0.6.0-py3-none-any.whl om --version` - `om, version 0.6.0`.

Known limitations:

- Publishing to PyPI and triggering Homebrew should only happen after the branch is merged or explicitly selected as the release commit, and after maintainer credentials/state are confirmed.

Next milestone:

- Release packaging and publication decision.
