# OM Cluster Sync Status

Implementation status for the local Codex worktree.

## Completed

- Phase 0: disabled-by-default feature flag, cluster path helpers, isolated HOME/XDG test fixture, disabled-mode regression tests.
- Phase 1: local append-only record store with canonical JSON, stable record IDs, Ed25519 signatures, ChaCha20-Poly1305 encryption, HLC ordering, atomic writes, heads, idempotent import, duplicate-ID tamper rejection, and private key permissions.
- Phase 2: filesystem transport and sync engine for untrusted shared directories, including two-temp-node convergence tests and shared-transport secrecy checks.
- Phase 3: cluster CLI for `init`, `invite`, `join`, `sync`, `status`, `peers`, `materialize`, `provenance`, `redact`, `revoke`, `rotate-key`, and profile/active overrides.
- Phase 4: materialization of observations, reflections, profile, and active views from records, snapshots, tombstones, and overrides.
- Phase 5: LAN discovery seam with privacy-preserving advertisement metadata; no required discovery dependency.
- Phase 6: direct P2P/relay extension point through the transport protocol; no heavyweight networking dependency added.
- Phase 7: README and operational/security documentation.

## Deferred Behind Seams

- Full interactive approval for invite/join. The current invite is a trusted direct invite with short expiration and documented sensitivity. Follow-up: [#34](https://github.com/intertwine/observational-memory/issues/34), targeted for `0.6.1`.
- Hosted relay transport. The sync engine accepts additional transports without changing record verification. Follow-up: [#35](https://github.com/intertwine/observational-memory/issues/35), targeted for `0.6.2`.
- Direct P2P transport. The sync engine accepts additional transports without changing record verification. Follow-up: [#36](https://github.com/intertwine/observational-memory/issues/36), targeted for `0.6.3`.
- Re-encryption of historical records during key rotation. Current rotation affects future records and distributes the new key through an encrypted key-rotation record. Follow-up: [#37](https://github.com/intertwine/observational-memory/issues/37), targeted for `0.6.4`.

## Verification

- `uv run pytest tests/sync -q`
- `uv run pytest tests/test_config.py tests/test_observe.py tests/test_reflect.py tests/test_cli_context.py -q`
- `uv run ruff check src/observational_memory/sync src/observational_memory/observe.py src/observational_memory/reflect.py src/observational_memory/cli.py src/observational_memory/config.py tests/sync tests/conftest.py`
