# Observational Memory Cluster Sync — Implementation Plan for Codex

**Audience:** Codex / coding agent implementing the feature in `intertwine/observational-memory`  
**Status:** Proposed implementation plan  
**Prepared:** 2026-05-08  
**Primary design goal:** Seamless, secure, decentralized, transport-agnostic synchronization of Observational Memory across multiple machines while preserving the existing local-first Markdown experience.

---

## 0. Executive summary

Observational Memory currently works well when all agents run on one machine because Claude Code, Codex, Cowork, and Hermes feed the same local memory directory. The current limitation is that memory is local per installation. Users who work with agents on multiple machines need memories to follow them without turning OM into a centralized hosted service.

The recommended design is **OM Cluster**:

1. Keep existing human-readable Markdown files as local materialized views.
2. Add a signed, encrypted, append-only cluster record log as the replicated source of truth.
3. Synchronize immutable records, not mutable Markdown files.
4. Rebuild `observations.md`, `reflections.md`, `profile.md`, `active.md`, and search indexes locally from records.
5. Implement filesystem transport first so users can use Syncthing, Dropbox, iCloud Drive, NAS mounts, rsync, USB drives, or any other shared directory without OM trusting that transport.
6. Add mDNS discovery, direct peer transports, relays, and CRDT-like editable overrides later, after the record model is correct.

The key architectural rule is:

> **Do not sync `observations.md`, `reflections.md`, `profile.md`, or `active.md` directly. Sync immutable memory records and materialize those Markdown files locally.**

This avoids file-level conflicts, preserves OM's inspectable local Markdown UX, and makes the sync layer transport-agnostic.

---

## 1. Current repository assumptions

Codex should verify these assumptions against the current checkout before implementing, but this plan is shaped around the repository state observed on 2026-05-08.

### 1.1 Existing product behavior

Observational Memory is a Python CLI package that gives Claude Code, Codex CLI, Cowork, and Hermes shared local memory across sessions. It writes local Markdown memory under the user's OM memory directory and exposes commands such as `om observe`, `om reflect`, `om search`, `om context`, `om backfill`, `om export`, `om install`, `om status`, and `om doctor`.

Important existing memory files:

```text
~/.local/share/observational-memory/
  observations.md
  reflections.md
  profile.md
  active.md
  .cursor.json
  .search-index/
  .scheduler-logs/
```

Important existing config file:

```text
~/.config/observational-memory/env
```

The env file contains provider credentials and should remain strictly local.

### 1.2 Existing memory tiers

Current README describes these memory tiers:

- Raw transcripts: session-local source material.
- Auto-memory: per-project Claude Code memory facts, scanned periodically.
- Observations: per-session and checkpoint notes, retained about 7 days.
- Reflections: durable long-term memory.
- Startup profile / active files: derived compact context.
- Platform exports: manual reviewed bundles.

This plan keeps that tiering. OM Cluster adds a replicated record layer underneath observations/reflections/startup context, rather than replacing the current UX.

### 1.3 Existing code areas likely affected

Expected relevant files and directories:

```text
src/observational_memory/config.py
src/observational_memory/cli.py
src/observational_memory/observe.py
src/observational_memory/reflect.py
src/observational_memory/startup_memory.py
src/observational_memory/search/
src/observational_memory/transcripts/
tests/
pyproject.toml
README.md
```

New code should mostly live under:

```text
src/observational_memory/sync/
```

Suggested submodules:

```text
src/observational_memory/sync/__init__.py
src/observational_memory/sync/atomic.py
src/observational_memory/sync/clock.py
src/observational_memory/sync/config.py
src/observational_memory/sync/crypto.py
src/observational_memory/sync/frontier.py
src/observational_memory/sync/materialize.py
src/observational_memory/sync/records.py
src/observational_memory/sync/store.py
src/observational_memory/sync/transports/__init__.py
src/observational_memory/sync/transports/filesystem.py
```

Optional later modules:

```text
src/observational_memory/sync/discovery/mdns.py
src/observational_memory/sync/transports/ssh.py
src/observational_memory/sync/transports/http_relay.py
src/observational_memory/sync/transports/iroh.py
src/observational_memory/sync/transports/libp2p.py
```

### 1.4 Dependency constraints

The project is Python 3.11+ and currently keeps dependencies small. Preserve that philosophy.

Recommended dependency additions:

```toml
cryptography >= 42.0.0
```

Optional extras only after the core phases pass:

```toml
[project.optional-dependencies]
discovery = ["zeroconf>=0.132.0"]
p2p = []  # Fill only if/when a Python-usable direct P2P transport is selected.
```

Do not add a heavy CRDT dependency in the first implementation. The append-only record log solves the urgent sync problem with less risk.

---

## 2. Design principles

### 2.1 Local-first, not hosted-first

OM Cluster should work with no cloud service. A user should be able to create a cluster, copy an invite to another machine, and sync through a shared folder or LAN. Relays and direct P2P can improve convenience later, but they should not be required.

### 2.2 Immutable records, derived Markdown

`observations.md`, `reflections.md`, `profile.md`, and `active.md` are useful local artifacts, but they are poor sync primitives. They are mutable and partly generated. Syncing them directly creates line-level conflicts that have no reliable semantic resolution.

Instead:

```text
transcripts / auto-memory / manual overrides
        ↓
observer / scanner / override command
        ↓
signed encrypted OM records
        ↓
cluster sync
        ↓
local materializer
        ↓
observations.md
reflections.md
profile.md
active.md
.search-index/
```

### 2.3 Transport-agnostic by construction

The sync engine should not care whether records arrive over a shared filesystem, SSH, HTTP relay, mDNS-discovered LAN peer, Syncthing, Dropbox, iCloud, rsync, or a future P2P stack.

Transport plugins should implement a small interface for exchanging heads and records.

### 2.4 Secure over untrusted transports

The transport should not need to be trusted. Records must be encrypted and signed before being handed to the transport. A shared folder provider should not see memory content. A malicious peer or corrupted folder should not be able to inject valid records without a node key.

### 2.5 Backward compatible by default

OM Cluster must be disabled by default until explicitly configured. Existing users should see no behavior change unless they run `om cluster init`, `om cluster join`, or set `OM_CLUSTER_ENABLED=1` after a valid cluster exists.

### 2.6 Fail open for local memory, fail closed for trust

If sync fails, OM should still run locally. If verification fails, OM must reject the untrusted record. Do not let sync failures prevent `om observe`, `om search`, or `om context` from working with local materialized memory.

---

## 3. Data model

### 3.1 Cluster directory layout

Local data directory:

```text
~/.local/share/observational-memory/
  observations.md                  # materialized local view
  reflections.md                   # materialized local view
  profile.md                       # materialized local view
  active.md                        # materialized local view
  .cursor.json                     # local-only
  .search-index/                   # local-only
  .scheduler-logs/                 # local-only
  clusters/
    <cluster-id>/
      cluster.json                 # public cluster metadata, no secrets
      records/
        <node-id>/
          00000000000000000001-<record-id>.omr.json
          00000000000000000002-<record-id>.omr.json
      heads/
        <node-id>.json
      nodes/
        <node-id>.json
      tombstones/
      materializer-state.json
```

Local config and secrets directory:

```text
~/.config/observational-memory/
  env                              # existing provider credentials; never sync
  cluster.toml                     # local cluster settings, no shared transport secrets if possible
  cluster-keys/
    <cluster-id>/
      node.json                    # node private keys; 0600
      cluster.key                  # cluster data key; 0600
```

Shared filesystem transport directory:

```text
<shared-path>/
  clusters/
    <cluster-id>/
      records/
        <node-id>/
          00000000000000000001-<record-id>.omr.json
      heads/
        <node-id>.json
      nodes/
        <node-id>.json
```

Do not put private keys or provider credentials in the shared transport directory.

### 3.2 Record kinds

Start with these record kinds:

```text
observation
reflection_snapshot
manual_override
tombstone
node_membership
```

Add later if needed:

```text
auto_memory_fact
namespace_policy
profile_review_state
```

### 3.3 Record envelope

Use one JSON file per record. The payload may be encrypted, but the outer envelope must contain enough clear metadata for routing, verification, and sync indexing.

Example envelope:

```json
{
  "version": 1,
  "cluster_id": "omc_7b4f6c...",
  "record_id": "sha256_...",
  "kind": "observation",
  "namespace": "personal",
  "node_id": "node_9fd2...",
  "node_seq": 42,
  "hlc": "2026-05-08T18:42:11.123456Z-0000-node_9fd2",
  "parents": {
    "node_9fd2": 41,
    "node_51ac": 77
  },
  "source": {
    "agent": "codex",
    "project": "observational-memory",
    "project_id": "git_sha256_abcd...",
    "transcript_id": "sha256_efgh...",
    "host_alias": "bryan-mbp"
  },
  "encryption": {
    "alg": "chacha20poly1305",
    "nonce": "base64url...",
    "aad_hash": "sha256_..."
  },
  "payload_ciphertext": "base64url...",
  "payload_hash": "sha256_...",
  "signature": {
    "alg": "ed25519",
    "key_id": "node_9fd2...",
    "sig": "base64url..."
  }
}
```

For implementation simplicity:

- Serialize payload plaintext as canonical JSON.
- Compute `payload_hash` from the canonical plaintext bytes.
- Encrypt canonical plaintext bytes with `ChaCha20Poly1305` using the cluster data key.
- The encryption AAD should include canonicalized clear metadata fields that must not be tampered with, especially `cluster_id`, `kind`, `namespace`, `node_id`, `node_seq`, `hlc`, and `parents`.
- Compute `record_id` from the canonical envelope body excluding `record_id` and `signature`.
- Sign the canonical envelope body excluding `signature` but including `record_id`.
- On read: verify hash, signature, membership, sequence, decrypt payload, verify payload hash.

This order prevents record ID/signature circularity while still making the record content-addressed and tamper-evident.

### 3.4 Payload schemas

#### Observation payload

```json
{
  "format": "markdown",
  "body": "## 2026-05-08\n\n### Observations\n- ...",
  "observed_at": "2026-05-08T18:42:11Z",
  "message_count": 18,
  "retention": "recent"
}
```

#### Reflection snapshot payload

```json
{
  "format": "markdown",
  "body": "# Reflections — Long-Term Memory\n\n*Last updated: ...*\n...",
  "frontier": {
    "node_9fd2": 42,
    "node_51ac": 77
  },
  "input_record_ids": ["sha256_...", "sha256_..."],
  "base_snapshot_ids": ["sha256_..."]
}
```

#### Manual override payload

```json
{
  "target": "profile",
  "section": "communication_style",
  "operation": "upsert",
  "body": "Bryan prefers direct technical recommendations with explicit tradeoffs."
}
```

#### Tombstone payload

```json
{
  "target_record_id": "sha256_...",
  "reason": "user-redaction",
  "created_at": "2026-05-08T19:00:00Z"
}
```

#### Node membership payload

```json
{
  "operation": "add",
  "node_id": "node_51ac...",
  "alias": "linux-devbox",
  "signing_public_key": "base64url...",
  "encryption_public_key": "base64url...",
  "created_at": "2026-05-08T19:10:00Z"
}
```

### 3.5 Hybrid logical clock

Implement a small HLC module so ordering is stable across machines even when wall clocks differ.

```python
@dataclass(frozen=True, order=True)
class HybridLogicalTimestamp:
    wall_time: datetime
    counter: int
    node_id: str
```

Rules:

- Local event: `wall = max(now, previous.wall)`. If `now <= previous.wall`, increment counter; else reset counter to zero.
- Remote event: merge local and remote clocks by HLC rules and persist the resulting local clock state.
- String format must sort lexicographically by wall time and counter:

```text
2026-05-08T18:42:11.123456Z-000004-node_9fd2
```

### 3.6 Frontier / vector clock

Use a frontier map to represent which records a node or snapshot has observed:

```json
{
  "node_9fd2": 42,
  "node_51ac": 77
}
```

Define:

```python
def frontier_covers(a: dict[str, int], b: dict[str, int]) -> bool:
    # True when a includes at least every node sequence included by b.
    return all(a.get(node, 0) >= seq for node, seq in b.items())
```

Reflection snapshots are comparable by frontier. If two snapshots have incomparable frontiers, neither should be line-merged. The reflector should create a new snapshot covering both.

---

## 4. Sync protocol

### 4.1 Local store responsibilities

The local store owns:

- Appending records for the local node.
- Importing remote records idempotently.
- Verifying signatures, membership, hashes, sequence monotonicity, encryption, and tombstones.
- Maintaining local heads.
- Listing missing records.
- Materializing local Markdown files.

### 4.2 Transport responsibilities

A transport only moves encrypted records and public heads between locations.

Suggested protocol interface:

```python
class SyncTransport(Protocol):
    name: str

    def list_heads(self, cluster_id: str) -> dict[str, int]: ...
    def read_head(self, cluster_id: str, node_id: str) -> dict | None: ...
    def publish_head(self, cluster_id: str, node_id: str, head: dict) -> None: ...

    def list_record_ids(self, cluster_id: str, node_id: str) -> set[str]: ...
    def push_record(self, cluster_id: str, node_id: str, record_id: str, data: bytes) -> None: ...
    def fetch_record(self, cluster_id: str, node_id: str, record_id: str) -> bytes | None: ...
```

### 4.3 Sync loop

```text
1. Load local cluster config and keys.
2. Load local heads.
3. For each configured transport:
   a. Publish local node metadata and head.
   b. List remote heads.
   c. For each remote node, find missing records.
   d. Fetch missing records.
   e. Verify and import records.
   f. Push local records missing from transport.
   g. Publish updated local head.
4. Materialize local Markdown files if records changed.
5. Reindex search if materialization changed.
```

### 4.4 Idempotence rules

- Importing the same record twice is a no-op.
- Pushing the same record twice is a no-op.
- Corrupt records are rejected and recorded in a local diagnostics log; they are not deleted from the transport automatically.
- Unknown future record versions are ignored with a warning.
- Records from revoked or unknown nodes are ignored unless they are valid membership records signed by an admin/current member under the chosen membership policy.

### 4.5 Locking and atomicity

Use atomic writes everywhere:

```text
write temp file in same directory
fsync file
rename temp to final path
fsync parent directory where practical
```

Use a coarse local lock for operations that modify local cluster state or materialized Markdown:

```text
~/.local/share/observational-memory/clusters/<cluster-id>/.locks/materialize.lock
~/.local/share/observational-memory/clusters/<cluster-id>/.locks/sync.lock
```

A portable lock can be implemented as atomic directory creation with stale lock cleanup.

---

## 5. Configuration model

### 5.1 Local cluster config file

Suggested path:

```text
~/.config/observational-memory/cluster.toml
```

Example:

```toml
[cluster]
enabled = true
id = "omc_7b4f6c..."
name = "Bryan Personal"
default_namespace = "personal"
sync_on_observe = true
sync_on_reflect = true
sync_before_context = true
startup_pull_deadline_ms = 1500
background_interval_seconds = 300
lan_interval_seconds = 30

[node]
id = "node_9fd2..."
alias = "bryan-mbp"
allowed_sources = ["claude", "codex", "cowork", "hermes", "claude-memory"]

[security]
encrypt_records = true
sign_records = true
allow_untrusted_transports = true

[merge]
observations = "append-only"
reflections = "frontier-snapshot"
profile = "derived-with-overrides"
active = "derived"
redactions = "tombstone"

[[transport]]
type = "filesystem"
path = "~/Sync/om-cluster"
```

Python 3.11 includes `tomllib` for reading TOML, but not writing TOML. Options:

- Read cluster config with `tomllib`.
- Write simple TOML manually for known fields.
- Add a lightweight TOML writer only if necessary.

Prefer manual writing in early phases.

### 5.2 Environment overrides

Add these optional environment variables:

```text
OM_CLUSTER_ENABLED=1
OM_CLUSTER_ID=omc_...
OM_CLUSTER_SYNC_ON_OBSERVE=1
OM_CLUSTER_SYNC_BEFORE_CONTEXT=1
OM_CLUSTER_STARTUP_PULL_DEADLINE_MS=1500
OM_CLUSTER_DEFAULT_NAMESPACE=personal
```

Env vars should override config file values for non-secret runtime behavior.

### 5.3 Local-only data

Never sync:

```text
~/.config/observational-memory/env
~/.config/observational-memory/cluster-keys/
~/.local/share/observational-memory/.cursor.json
~/.local/share/observational-memory/.search-index/
~/.local/share/observational-memory/.scheduler-logs/
~/.local/share/observational-memory/.qmd-docs/
```

---

## 6. CLI design

Add a `cluster` command group:

```bash
om cluster init --name "Bryan Personal" [--transport filesystem:~/Sync/om-cluster] [--import-existing]
om cluster invite --expires 10m
om cluster join <invite-token>
om cluster status [--json]
om cluster peers [--json]
om cluster sync [--transport filesystem] [--json]
om cluster materialize [--reindex]
om cluster provenance <query-or-record-id>
om cluster redact --record <record-id>
om cluster revoke <node-id>
om cluster rotate-key
```

Initial implementation can expose only:

```bash
om cluster init
om cluster status
om cluster sync
om cluster materialize
```

Then add invite/join once key and membership handling is solid.

### 6.1 `om cluster init`

Responsibilities:

- Create cluster ID.
- Create local node ID and alias.
- Generate node signing key.
- Generate cluster data encryption key.
- Write local config and secrets with `0600` permissions.
- Create local cluster store directories.
- Optionally import existing `observations.md` and `reflections.md` into records.
- Do not enable destructive materialization until a backup exists.

Suggested options:

```bash
om cluster init \
  --name "Bryan Personal" \
  --node-alias "bryan-mbp" \
  --transport filesystem:~/Sync/om-cluster \
  --import-existing
```

### 6.2 `om cluster status`

Show:

```text
Cluster: Bryan Personal (omc_...)
Node: bryan-mbp (node_...)
Enabled: true
Transports:
  filesystem ~/Sync/om-cluster reachable
Local records:
  observations: 18
  reflection snapshots: 2
  manual overrides: 0
Heads:
  node_9fd2 bryan-mbp seq=42 local
  node_51ac linux-devbox seq=77 last seen 2026-05-08T19:22Z
Materialized files:
  observations.md current
  reflections.md current
  profile.md current
  active.md current
Warnings:
  none
```

### 6.3 `om cluster sync`

Manual sync command. Must be safe to run repeatedly.

```bash
om cluster sync
```

Output should distinguish:

```text
Pulled 3 records from filesystem ~/Sync/om-cluster
Pushed 1 local record
Materialized observations.md, reflections.md, profile.md, active.md
Reindexed 24 documents
```

### 6.4 `om cluster materialize`

Rebuild local Markdown files from cluster records without network access.

```bash
om cluster materialize --reindex
```

This is important for debugging and recovery.

---

## 7. Phased implementation plan

The phases below are designed as small PRs. Each phase includes design intent, implementation tasks, acceptance criteria, and a validation rubric.

---

# Phase 0 — Baseline, guardrails, and test harness

## Goal

Prepare the repository for cluster work without changing runtime behavior.

## Why this phase exists

Sync touches persistence, CLI behavior, reflection, startup context, and search. Without a test harness and feature flag, implementation will be risky. The first PR should make it cheap to prove that existing OM remains unchanged when cluster sync is disabled.

## Implementation tasks

1. Add a feature flag helper:

   ```python
   def cluster_feature_enabled(config: Config) -> bool:
       ...
   ```

   This should return false unless a valid cluster config exists and either config or env enables it.

2. Add config properties:

   ```python
   @property
   def cluster_config_path(self) -> Path: ...

   @property
   def cluster_keys_dir(self) -> Path: ...

   @property
   def clusters_dir(self) -> Path: ...
   ```

3. Add empty `src/observational_memory/sync/` package.

4. Add test fixtures for isolated temp config/data homes:

   ```python
   @pytest.fixture
   def isolated_om_home(tmp_path, monkeypatch):
       monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
       monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
       ...
   ```

5. Add regression tests proving current behavior still works with cluster disabled:
   - `run_observer(..., dry_run=True)` unchanged.
   - `run_observer(..., dry_run=False)` writes `observations.md` as before.
   - `run_reflector(..., dry_run=True)` unchanged.
   - `om context` still reads/generates profile/active as before.

6. Add a `docs/` or `plans/` note summarizing the feature flag and non-goals.

## Acceptance criteria

- All existing tests pass.
- New tests pass with cluster disabled.
- No new runtime dependency is required yet.
- No user-visible CLI output changes except possibly hidden/internal helpers.
- `om observe`, `om reflect`, `om context`, `om search`, and `om doctor` behave identically with no cluster config.

## Validation rubric

| Area                     | Points | Pass condition                                                               |
| ------------------------ | -----: | ---------------------------------------------------------------------------- |
| Backward compatibility   |     35 | Existing commands behave identically when cluster is disabled.               |
| Test isolation           |     25 | Tests do not touch real home directories or user config.                     |
| Feature flag correctness |     20 | Cluster paths are not read/written unless enabled or explicitly initialized. |
| Code organization        |     10 | New sync package exists but does not pollute old modules.                    |
| Documentation            |     10 | Notes explain disabled-by-default behavior.                                  |

Minimum passing score: **90/100**. Any real-home write in tests is automatic failure.

---

# Phase 1 — Core record model, crypto, HLC, and local store

## Goal

Implement the local append-only cluster record store with deterministic hashing, signing, encryption, atomic writes, and idempotent reads. No networking yet.

## Why this phase exists

The record model is the foundation. If it is correct, transports become simple. If it is wrong, every transport and merge feature becomes fragile.

## Implementation tasks

### 1. Add `sync/atomic.py`

Functions:

```python
def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None: ...
def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None: ...
class DirectoryLock: ...
```

Requirements:

- Write temp file in same directory.
- Rename atomically.
- Apply `chmod` when requested.
- Best-effort `fsync` file and parent directory.
- Lock with atomic directory creation.
- Stale lock handling should be conservative.

### 2. Add `sync/clock.py`

Implement HLC parsing, formatting, local tick, and remote merge.

Tests:

- HLC strings sort correctly.
- Local monotonicity holds when wall clock moves backward.
- Remote merge moves local clock forward.

### 3. Add `sync/crypto.py`

Use `cryptography`.

Functions/classes:

```python
@dataclass(frozen=True)
class NodeKeypair:
    node_id: str
    signing_private_key_b64: str
    signing_public_key_b64: str

@dataclass(frozen=True)
class ClusterSecret:
    cluster_id: str
    data_key_b64: str

def generate_node_keypair(alias: str | None = None) -> NodeKeypair: ...
def generate_cluster_secret() -> ClusterSecret: ...
def sign_ed25519(private_key_b64: str, data: bytes) -> str: ...
def verify_ed25519(public_key_b64: str, data: bytes, signature_b64: str) -> bool: ...
def encrypt_payload(data_key_b64: str, plaintext: bytes, aad: bytes) -> EncryptedPayload: ...
def decrypt_payload(data_key_b64: str, encrypted: EncryptedPayload, aad: bytes) -> bytes: ...
```

Recommended primitives:

- Ed25519 for signatures.
- ChaCha20-Poly1305 for authenticated encryption.
- Base64url without newlines for serialized keys and ciphertext.

### 4. Add `sync/records.py`

Define dataclasses and canonical JSON helpers.

```python
def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
```

Core APIs:

```python
@dataclass(frozen=True)
class RecordEnvelope: ...

def create_record(...): ...
def verify_record(...): ...
def decrypt_record_payload(...): ...
def record_path_name(record: RecordEnvelope) -> str: ...
```

Validation rules:

- `version == 1`.
- `cluster_id` matches local cluster.
- `record_id` matches canonical body hash.
- `signature` verifies against known node public key.
- `payload_hash` matches decrypted payload.
- `node_seq` is positive integer.
- `kind` is known or safely ignored.

### 5. Add `sync/store.py`

Core class:

```python
class ClusterStore:
    def __init__(self, config: Config, cluster_config: ClusterConfig): ...

    def append_record(self, kind: str, namespace: str, source: dict, payload: dict) -> RecordEnvelope: ...
    def import_record_bytes(self, data: bytes) -> ImportResult: ...
    def list_records(self, kind: str | None = None, include_tombstoned: bool = False) -> list[RecordEnvelope]: ...
    def read_payload(self, record: RecordEnvelope) -> dict: ...
    def local_head(self) -> dict: ...
    def all_heads(self) -> dict[str, int]: ...
```

Store state:

```text
heads/<node-id>.json
nodes/<node-id>.json
materializer-state.json
```

### 6. Add local cluster config types

`sync/config.py` should load/write cluster config and keys.

```python
@dataclass
class ClusterConfig:
    enabled: bool
    id: str
    name: str
    default_namespace: str
    node_id: str
    node_alias: str
    transports: list[TransportConfig]
    sync_on_observe: bool
    sync_on_reflect: bool
    sync_before_context: bool
```

### 7. Tests

Add tests under `tests/sync/`:

```text
test_canonical_json.py
test_hlc.py
test_crypto.py
test_records.py
test_store.py
```

## Acceptance criteria

- Can create a cluster config and keys in an isolated temp home.
- Can append an encrypted signed observation record locally.
- Can read/decrypt the record and recover payload.
- Tampering with metadata, ciphertext, payload hash, record id, or signature causes verification failure.
- Importing the same record twice is idempotent.
- Local heads update monotonically.
- No plaintext memory body appears in record files when encryption is enabled.
- Private keys and cluster data key are written with `0600` permissions.

## Validation rubric

| Area                          | Points | Pass condition                                                    |
| ----------------------------- | -----: | ----------------------------------------------------------------- |
| Deterministic record identity |     20 | Record IDs are stable and verification recomputes them correctly. |
| Cryptographic integrity       |     25 | Tamper tests fail closed; signatures and AEAD are verified.       |
| Confidentiality               |     15 | Encrypted record files do not contain plaintext payload bodies.   |
| HLC / sequencing              |     15 | Ordering is deterministic and monotonic per node.                 |
| Atomic storage                |     10 | Partial writes do not leave valid-looking corrupt records.        |
| Idempotence                   |     10 | Duplicate imports are harmless.                                   |
| Tests/docs                    |      5 | APIs are documented enough for later phases.                      |

Minimum passing score: **90/100**. Any accepted tampered record is automatic failure.

---

# Phase 2 — Cluster CLI init/status/materialize skeleton

## Goal

Expose enough CLI to create a local cluster, inspect it, and rebuild local Markdown from records. Still no external sync.

## Why this phase exists

Users and tests need a supported way to initialize cluster state. Codex also needs a stable CLI surface before changing observer/reflector behavior.

## Implementation tasks

### 1. Add CLI group

In `cli.py`:

```python
@cli.group()
def cluster(): ...
```

Add commands:

```python
cluster init
cluster status
cluster materialize
```

### 2. Implement `om cluster init`

Options:

```bash
om cluster init \
  --name TEXT \
  --node-alias TEXT \
  --default-namespace TEXT \
  --transport filesystem:PATH \
  --import-existing/--no-import-existing
```

Behavior:

- Refuse to overwrite existing cluster unless `--force` is supplied.
- Create local config and keys.
- Add local node metadata.
- Create an initial `node_membership` record for the local node.
- If `--import-existing`, import existing `observations.md` and `reflections.md` as cluster records.

Import behavior:

- Existing `observations.md`: create one `observation` record per date section if simple to parse; otherwise one legacy import observation record.
- Existing `reflections.md`: create a `reflection_snapshot` record with empty or imported frontier. Mark source as `legacy-local-import`.
- Existing `profile.md` and `active.md`: do not import as source-of-truth because they are generated.
- Before any materialized overwrite, copy existing Markdown files to:

```text
~/.local/share/observational-memory/backups/cluster-init-<timestamp>/
```

### 3. Implement `om cluster status`

Must work even if cluster is not initialized.

`--json` should return machine-readable status.

### 4. Implement `om cluster materialize`

Add `sync/materialize.py`.

Initial behavior:

- Materialize observation records into `observations.md`.
- If reflection snapshot records exist, choose best snapshot and write `reflections.md`.
- Run existing `refresh_startup_memory(config)` to regenerate `profile.md` and `active.md`.
- Reindex search unless disabled or `--no-reindex` is passed.

Observation rendering:

```markdown
# Observations

## 2026-05-08

### From bryan-mbp / codex / project:observational-memory

- ...
```

The materializer must preserve source provenance in a readable but compact way.

### 5. Add generated-file headers

When cluster materializes a file, include a header:

```markdown
<!-- Generated by Observational Memory Cluster. Do not edit directly. -->
<!-- To make durable manual changes, use profile overrides or disable cluster materialization. -->
```

Only add this to generated files when cluster mode is enabled.

## Acceptance criteria

- `om cluster init` creates a valid local cluster.
- `om cluster status` accurately reports initialized/uninitialized state.
- `om cluster materialize` creates valid Markdown from local records.
- `--import-existing` preserves existing memory through backup and import.
- Generated `profile.md` and `active.md` remain usable by `om context`.
- Cluster disabled behavior remains unchanged.

## Validation rubric

| Area                        | Points | Pass condition                                                  |
| --------------------------- | -----: | --------------------------------------------------------------- |
| CLI ergonomics              |     20 | Commands are clear, helpful, and fail with actionable messages. |
| Safe initialization         |     20 | Existing files are backed up before cluster materialization.    |
| Materialization correctness |     25 | Records produce deterministic Markdown with provenance.         |
| Backward compatibility      |     15 | Non-cluster installs behave unchanged.                          |
| Search/startup integration  |     10 | Startup memory refresh and search reindex still work.           |
| Tests                       |     10 | CLI tests cover init/status/materialize.                        |

Minimum passing score: **85/100**.

---

# Phase 3 — Observation integration and local materialized views

## Goal

When cluster mode is enabled, `om observe` should write observation records instead of directly mutating `observations.md`, then materialize local Markdown from the record store.

## Why this phase exists

This is the first phase where OM Cluster becomes useful locally. It also verifies that the record model can replace direct observation writes without breaking the existing UX.

## Implementation tasks

### 1. Add cluster-aware write path in `observe.py`

Current observer flow writes or appends observations to `observations.md`. Change this conditionally:

```python
if cluster_feature_enabled(config):
    store = ClusterStore.from_config(config)
    record = store.append_record(
        kind="observation",
        namespace=namespace_for_source(...),
        source=source_metadata(...),
        payload={"format": "markdown", "body": result, ...},
    )
    materialize_cluster_memory(config, store)
    reindex_if_enabled(config)
else:
    existing behavior
```

Preserve dry-run behavior exactly: dry-run must not write records or Markdown.

### 2. Add source metadata builder

Create `sync/source.py` or place in `records.py` initially.

Metadata should include:

```text
agent: claude | codex | cowork | hermes | claude-memory | manual | legacy-local-import
host_alias: configured local node alias
project: best-effort project slug
project_id: hashed git remote or hashed path, when available
transcript_id: hash of transcript path + maybe content hash, not raw path in cleartext
cwd_hash: optional
```

Avoid exposing raw local paths in clear metadata. If raw path is useful, put it inside encrypted payload.

### 3. Namespace resolver

Add simple resolver:

```python
def namespace_for_source(config, source: str, transcript_path: Path | None = None) -> str:
    return cluster_config.default_namespace
```

Later phases can add rules. Start simple.

### 4. Observation materializer

Sort records by:

```text
observed date
HLC
data source
node id
node sequence
record id
```

Deduplicate by `record_id`. Optionally deduplicate exact same payload hash.

Respect retention for the materialized `observations.md` view:

- Default: show records from the last `config.observation_retention_days` days.
- But do not delete old records from the cluster log.
- Existing reflector trimming should not delete cluster records.

### 5. Reflector catchup compatibility

`_maybe_run_reflector_catchup` should still work. It can read materialized `observations.md` for now. Phase 5 will make reflector cluster-native.

### 6. Tests

Add tests:

- Cluster enabled observe creates an observation record.
- Cluster enabled observe materializes `observations.md`.
- Cluster enabled dry-run creates no record.
- Cluster disabled observe continues old direct file write path.
- Source metadata does not leak raw transcript path in clear record envelope.

## Acceptance criteria

- `om observe` with cluster enabled appends a signed encrypted record and materializes `observations.md`.
- Existing `om context` sees the generated startup memory after observe.
- Existing `om search` finds materialized observations after reindex.
- `observations.md` is deterministic when materialized repeatedly from the same records.
- Old observations are omitted from the materialized recent view after retention but remain in cluster records.
- No plaintext observation body appears in `.omr.json` files.

## Validation rubric

| Area                           | Points | Pass condition                                                      |
| ------------------------------ | -----: | ------------------------------------------------------------------- |
| Observe integration            |     25 | Cluster path replaces direct write only when enabled.               |
| Materialized Markdown UX       |     20 | Output is readable, deterministic, and source-attributed.           |
| Dry-run/backward compatibility |     20 | Dry-run and disabled mode are unchanged.                            |
| Privacy                        |     15 | Clear record metadata does not leak sensitive raw paths by default. |
| Search/startup continuity      |     10 | Existing search/context remain useful.                              |
| Tests                          |     10 | Unit and CLI tests cover new path.                                  |

Minimum passing score: **90/100**.

---

# Phase 4 — Filesystem transport and two-node sync

## Goal

Implement actual multi-machine sync through a shared filesystem transport.

## Why this phase exists

Filesystem transport gives the most value with the least network complexity. Users can pair OM Cluster with Syncthing, Dropbox, iCloud Drive, a NAS, rsync, Tailscale-mounted storage, or USB transfer. Since OM records are encrypted and signed, the shared path does not need to be trusted.

## Implementation tasks

### 1. Add transport abstraction

`sync/transports/__init__.py`:

```python
class SyncTransport(Protocol):
    ...
```

`sync/transports/filesystem.py`:

```python
class FilesystemTransport:
    def __init__(self, root: Path): ...
    def list_heads(...): ...
    def publish_head(...): ...
    def list_record_ids(...): ...
    def push_record(...): ...
    def fetch_record(...): ...
```

### 2. Shared transport layout

```text
<transport-root>/
  clusters/
    <cluster-id>/
      records/
        <node-id>/
          <seq>-<record-id>.omr.json
      heads/
        <node-id>.json
      nodes/
        <node-id>.json
```

### 3. Add sync engine

`sync/engine.py`:

```python
@dataclass
class SyncSummary:
    pulled: int
    pushed: int
    skipped: int
    rejected: int
    materialized: bool
    transports: list[TransportSummary]

def sync_cluster(config: Config, deadline_ms: int | None = None) -> SyncSummary: ...
```

### 4. Implement push/pull

For each configured transport:

- Publish local node metadata.
- Publish local head.
- Pull remote node metadata.
- Pull records not present locally.
- Import records with verification.
- Push local records not present remotely.
- Publish updated local head.

### 5. Membership bootstrap for filesystem v1

For v1, allow invite/join to be deferred. Tests can simulate two nodes that share cluster secret and trust each other's node metadata.

However, production `om cluster init` should not silently trust arbitrary nodes found in a shared folder. Options:

- Early simple mode: nodes must be manually added through a local `nodes/<node-id>.json` copied by `om cluster join`.
- Proper mode later: `node_membership` records signed by an existing node/admin.

Do not accept records from unknown nodes just because they appear in the transport.

### 6. Add `om cluster sync`

CLI should call sync engine and print summary.

Options:

```bash
om cluster sync --json
om cluster sync --no-materialize
```

### 7. Tests

Create two isolated OM homes and one shared transport directory:

```text
tmp/node-a-home
tmp/node-b-home
tmp/shared-transport
```

Test flow:

1. Initialize cluster on node A.
2. Initialize node B with same cluster ID/secret and registered node metadata using a test helper.
3. Node A observes memory A.
4. Node B observes memory B.
5. Both sync through filesystem transport.
6. Both materialized `observations.md` contain A and B records.
7. Record files in shared transport contain no plaintext memory body.

## Acceptance criteria

- Two local test nodes can exchange encrypted signed observation records through a shared directory.
- Sync is idempotent.
- Sync works regardless of push/pull order.
- Unknown or tampered remote records are rejected.
- Shared transport contains no private keys and no plaintext payloads.
- `om cluster sync` gives understandable output.

## Validation rubric

| Area                 | Points | Pass condition                                                            |
| -------------------- | -----: | ------------------------------------------------------------------------- |
| Two-node correctness |     30 | Both nodes converge to the same valid materialized observations.          |
| Idempotence          |     15 | Repeated syncs produce no duplicates or spurious changes.                 |
| Security             |     20 | Unknown/tampered records are rejected; payloads are encrypted.            |
| Transport isolation  |     10 | Transport code does not depend on local store internals beyond interface. |
| CLI usability        |     10 | Manual sync output is actionable.                                         |
| Tests                |     15 | Multi-node fixture covers realistic sync.                                 |

Minimum passing score: **90/100**. Accepting unknown-node records is automatic failure unless explicitly in a documented insecure test mode.

---

# Phase 5 — Reflection snapshots and semantic merge

## Goal

Make `reflections.md` cluster-safe by storing reflection outputs as `reflection_snapshot` records with input frontiers, then materializing the best snapshot locally.

## Why this phase exists

`reflections.md` is the most conflict-prone file. Two machines can observe different transcripts and independently produce different reflections. A text merge cannot reliably decide which durable memory is correct. A snapshot/frontier model makes conflicts explicit and lets the reflector perform semantic consolidation.

## Implementation tasks

### 1. Add frontier utilities

`sync/frontier.py`:

```python
def frontier_from_records(records: Iterable[RecordEnvelope]) -> dict[str, int]: ...
def frontier_covers(a: dict[str, int], b: dict[str, int]) -> bool: ...
def frontier_join(*frontiers: dict[str, int]) -> dict[str, int]: ...
def frontier_compare(a, b) -> Literal["covers", "covered_by", "equal", "incomparable"]: ...
```

### 2. Materialize reflection snapshots

Rules:

- Ignore tombstoned snapshots.
- Choose the snapshot that covers the greatest frontier.
- If multiple snapshots have equal/incomparable coverage, choose the one with largest total covered sequence count, then latest HLC, then record ID.
- If there are known observations not covered by the selected snapshot, materialize selected snapshot as `reflections.md` but mark catchup needed.

### 3. Cluster-aware `run_reflector`

When cluster enabled:

1. Load observation records.
2. Load reflection snapshots.
3. Select best base snapshot(s).
4. Determine observations not covered by best snapshot frontier.
5. If no uncovered observations and no changed auto-memory, return `None`.
6. If competing snapshots are incomparable, include their bodies as candidate bases in the reflector prompt.
7. Run existing reflector prompt/LLM flow.
8. Stamp timestamps.
9. Create a new `reflection_snapshot` record with frontier covering all input observations and base snapshots.
10. Materialize `reflections.md`, `profile.md`, `active.md`.
11. Reindex.

### 4. Prompt adjustment

If multiple base snapshots exist, prepend a small instruction:

```text
You are merging durable memory snapshots from multiple machines. Preserve durable facts, reconcile duplicates, and prefer newer explicit corrections. Do not include source-machine chatter unless it is itself useful memory.
```

Do not overcomplicate prompt changes; preserve the existing reflector behavior as much as possible.

### 5. Observation trimming behavior

Existing reflector trims old observations from `observations.md`. In cluster mode:

- Do not delete observation records.
- Materializer controls how much recent observation text appears in `observations.md`.
- Trimming materialized `observations.md` is okay because it is derived.

### 6. Tests

Scenarios:

1. Single node observations -> reflect -> snapshot created -> `reflections.md` materialized.
2. Two nodes each create observations -> each creates independent snapshot -> sync -> reflector creates consolidation snapshot that covers both frontiers.
3. Snapshot A covers B -> materializer chooses A.
4. Incomparable snapshots -> catchup needed.
5. Dry-run reflector does not write snapshot.

Mock LLM compression to deterministic outputs.

## Acceptance criteria

- `om reflect` in cluster mode creates a `reflection_snapshot` record, not only a mutable `reflections.md` write.
- `reflections.md` is generated from the selected snapshot.
- Concurrent/incomparable snapshots are not line-merged.
- A later reflection can consolidate competing snapshots and produce a frontier covering both.
- Old direct reflector behavior remains unchanged when cluster disabled.

## Validation rubric

| Area                   | Points | Pass condition                                                     |
| ---------------------- | -----: | ------------------------------------------------------------------ |
| Snapshot correctness   |     25 | Snapshot payload includes body, frontier, and base/input metadata. |
| Conflict handling      |     25 | Incomparable snapshots are detected and semantically consolidated. |
| Backward compatibility |     15 | Non-cluster reflector behavior remains unchanged.                  |
| Materialization        |     15 | `reflections.md`, `profile.md`, and `active.md` are deterministic. |
| Tests                  |     20 | Multi-node competing-reflection tests pass with mocked LLM.        |

Minimum passing score: **90/100**.

---

# Phase 6 — Namespaces, provenance, redactions, and profile overrides

## Goal

Add richer source attribution, namespace policy, redaction/tombstone commands, and safe conflict handling for `profile.md`-like manual support files.

## Why this phase exists

Users need to know where memories came from, keep work/personal/org/project memories separate, remove sensitive records, and preserve manual preferences without editing generated files directly.

## Implementation tasks

### 1. Namespace resolver v2

Add a namespace config section:

```toml
[namespaces]
default = "personal"

[[namespace_rule]]
source = "codex"
path_contains = "/work/expel/"
namespace = "work:expel"

[[namespace_rule]]
source = "claude"
git_remote_hash = "sha256_..."
namespace = "project:observational-memory"
```

Implement:

```python
def namespace_for_event(source: SourceEvent, cluster_config: ClusterConfig) -> str: ...
```

Rules should be deterministic. If no rule matches, use default namespace.

### 2. Provenance search

Add:

```bash
om cluster provenance <record-id>
om cluster provenance --query "sync design"
```

At minimum, `--record-id` should print:

```text
Record: sha256_...
Kind: observation
Namespace: project:observational-memory
Node: bryan-mbp (node_...)
Agent: codex
Project: observational-memory
Observed: 2026-05-08T18:42:11Z
Transcript: sha256_...
Payload hash: sha256_...
```

For query mode, reuse search results and map them back to record metadata if possible.

### 3. Tombstones/redaction

Add:

```bash
om cluster redact --record <record-id> [--reason TEXT]
```

Behavior:

- Create a `tombstone` record targeting the record ID.
- Materializer excludes tombstoned records.
- Search reindex removes tombstoned content from local search.
- Do not physically delete by default.

Optional later:

```bash
om cluster purge-tombstoned --local-only
```

Caveat to document: tombstones prevent future use but cannot guarantee that already-synced machines or backups forget data they have previously stored.

### 4. Profile override records

Do not sync `profile.md` directly. Instead add manual override records.

Commands:

```bash
om cluster override add --target profile --section communication_style --body "..."
om cluster override list
om cluster override remove <override-record-id>
```

Materialization rule:

```text
profile.md = derived profile from reflections/observations + active manual override records
active.md = derived active work + optional active override records
```

If two overrides target the same section:

- If identical payload body: deduplicate.
- If different: include both in a generated “Manual overrides requiring review” section or choose latest HLC and preserve older in provenance output.

### 5. Source-aware materialization

Observation/reflection rendering should be able to filter by namespace later. For v1, include compact namespace/source tags in observations and provenance metadata in search documents.

### 6. Tests

- Namespace rule matches expected source.
- Unknown namespace falls back to default.
- Redacted record disappears from `observations.md` and search after reindex.
- Tombstone syncs to second node and hides target there.
- Profile override appears in generated `profile.md`.
- Conflicting overrides are not silently lost.

## Acceptance criteria

- Source namespaces are assigned deterministically and visible in materialized observations/provenance.
- Redaction creates tombstones and removes content from materialized/search outputs.
- `profile.md` is no longer treated as a sync source of truth.
- Manual durable profile changes can be represented by override records.
- Conflicting overrides are surfaced, not silently overwritten.

## Validation rubric

| Area                      | Points | Pass condition                                                              |
| ------------------------- | -----: | --------------------------------------------------------------------------- |
| Namespace correctness     |     20 | Rules are deterministic and tested.                                         |
| Provenance                |     15 | Users can inspect where a memory came from.                                 |
| Redaction behavior        |     25 | Tombstoned records disappear from materialized/search outputs across nodes. |
| Profile conflict handling |     20 | Overrides are represented as records and conflicts are visible.             |
| Privacy/security          |     10 | Provenance avoids unnecessary raw path leakage.                             |
| Tests/docs                |     10 | User-facing caveats are documented.                                         |

Minimum passing score: **85/100**. Silently dropping conflicting overrides is automatic failure.

---

# Phase 7 — Sync cadence, startup pull, scheduler integration, and diagnostics

## Goal

Make sync feel seamless by adding safe automatic sync triggers around observe, reflect, context, and background scheduler runs.

## Why this phase exists

Manual `om cluster sync` proves the protocol, but the user goal is seamless memory sharing across machines. Agents should usually start with recent memories from the cluster without requiring the user to remember a command.

## Implementation tasks

### 1. Sync triggers

Add these optional triggers when cluster enabled:

```text
After observe writes a local record: quick push/pull.
After reflect writes a snapshot: quick push/pull.
Before om context returns startup context: quick pull with short deadline.
Background scheduler: anti-entropy sync every N minutes.
Manual: om cluster sync.
```

### 2. Deadline-aware sync

`om context` is latency-sensitive. Add:

```python
sync_cluster(config, deadline_ms=cluster_config.startup_pull_deadline_ms, pull_only=True)
```

Default startup deadline: 1500 ms.

If deadline expires:

- Return best local context.
- Print nothing to stdout that would corrupt hook JSON.
- Optionally log diagnostic to scheduler log or cluster diagnostics.

### 3. Install/scheduler changes

Extend install only after manual sync is stable.

Options:

```bash
om install --cluster-sync
om install --scheduler auto --cluster-sync
```

Alternatively, do not modify install yet; use existing scheduler jobs to call `om cluster sync` in a separate phase.

### 4. Sync-on-observe/reflect

After local record creation:

- Run sync in best-effort mode.
- Do not fail observe/reflect if sync fails.
- Do not block too long.
- Re-materialize only if remote records were pulled.

### 5. Diagnostics

Extend:

```bash
om doctor
om cluster status
```

Diagnostics should check:

- Cluster config valid.
- Keys exist and permissions are safe.
- Transport path reachable.
- Local head consistent with records.
- Unknown/rejected records count.
- Materialized files current.
- Search index age/currentness.

### 6. Tests

- `om context` with cluster enabled performs pull before context with mocked fast transport.
- `om context` with slow transport returns local context without invalid JSON.
- `om observe` sync failure does not lose local observation record.
- `om reflect` sync failure does not lose local snapshot.
- `om doctor --json` reports cluster state.

## Acceptance criteria

- Automatic sync can be enabled without making hooks brittle.
- Startup context pulls remote memories opportunistically within deadline.
- Local observe/reflect remain reliable when network/transport is down.
- Diagnostics clearly identify sync problems.
- No hook command emits invalid JSON due to sync logs on stdout.

## Validation rubric

| Area                  | Points | Pass condition                                                 |
| --------------------- | -----: | -------------------------------------------------------------- |
| Seamlessness          |     25 | Common observe/reflect/context flows sync automatically.       |
| Latency control       |     20 | Startup pull respects deadline and never corrupts hook output. |
| Reliability           |     20 | Transport failures do not break local memory.                  |
| Diagnostics           |     15 | `doctor`/`status` identify common misconfigurations.           |
| Scheduler integration |     10 | Background sync can be installed or documented safely.         |
| Tests                 |     10 | Timeout/failure tests cover hook-sensitive paths.              |

Minimum passing score: **90/100**. Corrupting `om context` JSON is automatic failure.

---

# Phase 8 — Auto-discovery and direct peer options

## Goal

Add optional LAN discovery and, later, optional direct P2P transports without changing the core sync engine.

## Why this phase exists

Auto-discovery is a nice-to-have. It should not block the core design. Once records and sync are transport-agnostic, discovery only supplies transport hints.

## Implementation tasks

### 1. mDNS discovery

Optional extra:

```bash
uv tool install "observational-memory[discovery]"
```

Advertise:

```text
service: _om-sync._tcp.local
cluster_hash: sha256(cluster_id)
node_id: node_...
protocol: om-sync/1
port: local ephemeral sync port
```

Rules:

- Discovery never implies trust.
- Do not expose cluster name or user name in broadcast by default.
- Only sync with already-authorized nodes.

### 2. Local peer HTTP/QUIC server

A minimal local peer server can expose encrypted record exchange:

```text
GET /heads
GET /records/<node-id>/<record-id>
PUT /records/<node-id>/<record-id>
```

Still verify all records locally.

### 3. Direct P2P transport

Evaluate Iroh/libp2p/Tailscale-local options. Keep this as an optional extra. Do not make it required for normal OM.

Selection criteria:

- Python usability.
- Encrypted transport.
- NAT traversal or relay support.
- Low install friction.
- Works well from CLI and scheduler contexts.
- Does not require a long-running daemon unless explicitly installed.

### 4. Tests

- Discovery only advertises cluster hash, not cluster name.
- Unauthorized discovered peer is ignored.
- Authorized discovered peer can sync records.
- Discovery unavailable falls back to configured transports.

## Acceptance criteria

- mDNS discovery can find peers on LAN without auto-trusting them.
- Discovery failures do not affect filesystem sync.
- Direct P2P remains optional and separately installable.
- Core sync tests remain independent of discovery/P2P dependencies.

## Validation rubric

| Area             | Points | Pass condition                                         |
| ---------------- | -----: | ------------------------------------------------------ |
| Privacy          |     25 | Broadcast metadata is minimal and non-sensitive.       |
| Trust separation |     25 | Discovery never bypasses membership verification.      |
| Optionality      |     20 | Core package works without discovery/P2P dependencies. |
| Functionality    |     20 | Authorized LAN peers can sync in integration tests.    |
| Documentation    |     10 | Limitations and setup are clear.                       |

Minimum passing score: **85/100**.

---

# Phase 9 — Invite/join, revocation, key rotation, and hardening

## Goal

Make multi-machine cluster setup secure and easy for non-expert users.

## Why this phase exists

Manual copying of cluster keys is acceptable for early tests but not a good UX or security model. Users need clear pairing, revocation, and emergency recovery.

## Implementation tasks

### 1. Invite tokens

Command:

```bash
om cluster invite --expires 10m
```

Token contains:

```text
cluster id
cluster public metadata
bootstrap transport hints
one-time join secret
expiration
optional namespace restrictions
```

Format:

```text
omc1:<base64url-cbor-or-json>
```

Keep token human-copyable. Do not include long-term private keys unless there is no alternative. For filesystem-only v1, a join token may include encrypted cluster key material protected by a one-time secret.

### 2. Join flow

Command:

```bash
om cluster join <invite-token>
```

Flow:

1. Parse token.
2. Create local node keypair.
3. Connect to bootstrap transport or write join request file.
4. Existing node approves/signs membership record.
5. New node stores cluster key and config.
6. Run initial sync.

If fully interactive approval is too much for v1, support a direct trusted invite that includes enough encrypted key material to join. Document risk clearly.

### 3. Revocation

Command:

```bash
om cluster revoke <node-id>
```

Creates membership/tombstone record marking node revoked.

Rules:

- Ignore future records from revoked node with HLC/seq after revocation.
- Keep historical records unless explicitly redacted.
- Warn user to rotate cluster key if device compromise is suspected.

### 4. Key rotation

Command:

```bash
om cluster rotate-key
```

Minimum v1 behavior:

- Generate new data key.
- Use new key for future records.
- Distribute to active nodes through membership/key records.

Optional stronger behavior:

- Re-encrypt existing records not tombstoned.

### 5. Security hardening tests

- Invite expires.
- Revoked node's new record is rejected.
- Old historical record from revoked node remains valid if created before revocation.
- Key rotation produces records decryptable by active nodes and not by revoked node in test harness.

## Acceptance criteria

- Users can add a second machine without manually copying hidden key files.
- Revoked nodes cannot add accepted future records.
- Key rotation exists for compromised/lost devices.
- Pairing UX is documented and clear.

## Validation rubric

| Area                | Points | Pass condition                                                |
| ------------------- | -----: | ------------------------------------------------------------- |
| Setup UX            |     20 | Invite/join is understandable and low-friction.               |
| Membership security |     25 | Unknown/revoked nodes cannot inject future records.           |
| Key handling        |     25 | Keys are stored safely and rotation works for future records. |
| Recovery            |     10 | User can inspect and repair cluster status.                   |
| Tests               |     20 | Invite, revoke, and rotate flows are covered.                 |

Minimum passing score: **90/100**. Leaking long-term secrets in ordinary shared transport files is automatic failure.

---

## 8. Detailed integration notes

### 8.1 `observe.py`

Add a single cluster branch around write behavior. Do not fork the whole observer logic.

Desired shape:

```python
def run_observer(messages, config=None, dry_run=False):
    ...
    result = compress(...)
    if dry_run:
        return result

    if cluster_feature_enabled(config):
        _write_observation_record(result, messages, config)
    else:
        _write_observations(result, config)

    return result
```

The helper should:

- Derive source metadata from messages and caller context.
- Append record.
- Materialize.
- Reindex.
- Best-effort sync if Phase 7 is implemented.

Keep existing `_write_observations` for disabled mode.

### 8.2 `reflect.py`

Keep existing direct file reflection for disabled mode. Add cluster-aware branch at the top:

```python
def run_reflector(config=None, dry_run=False):
    if config is None:
        config = Config()
    if cluster_feature_enabled(config):
        return run_cluster_reflector(config, dry_run=dry_run)
    ... existing behavior ...
```

This isolates the complexity of reflection snapshots.

### 8.3 `startup_memory.py`

Avoid major changes unless needed. Let the materializer write `reflections.md` and `observations.md`, then call existing startup refresh.

If `profile.md` and `active.md` currently assume `reflections.md` and `observations.md`, keep that contract.

Later, inject overrides by either:

- Adding override text to the inputs before calling existing generation, or
- Appending an override section after generation.

Prefer the simpler append approach first.

### 8.4 `search/`

Search should remain over materialized files. Do not make search index a sync artifact.

When cluster materialization changes content, call the existing reindex function. Never sync `.search-index`.

Add record provenance metadata to indexed chunks only if it can be done cleanly. If not, provenance query can inspect records directly.

### 8.5 `config.py`

Add path helpers only. Do not overload the existing LLM env file with cluster secrets.

### 8.6 `cli.py`

Keep new cluster commands grouped. Avoid adding many top-level commands.

When `om context` runs automatic sync, do not print sync logs to stdout because stdout must be valid hook JSON. Use stderr or a diagnostics log only if safe.

---

## 9. Migration strategy

### 9.1 Fresh install

```bash
om install
om cluster init --name "Bryan Personal" --transport filesystem:~/Sync/om-cluster
```

Cluster mode is enabled after init.

### 9.2 Existing install

```bash
om cluster init --name "Bryan Personal" --import-existing --transport filesystem:~/Sync/om-cluster
```

Expected behavior:

1. Back up current Markdown files.
2. Create cluster keys/config.
3. Import existing observations/reflections as records.
4. Materialize equivalent Markdown.
5. Reindex.
6. Show summary and rollback instructions.

### 9.3 Rollback

Users can disable cluster mode:

```bash
OM_CLUSTER_ENABLED=0 om context
```

Or edit:

```text
~/.config/observational-memory/cluster.toml
```

Set:

```toml
[cluster]
enabled = false
```

The current materialized Markdown files remain readable. If necessary, restore backups from:

```text
~/.local/share/observational-memory/backups/cluster-init-<timestamp>/
```

---

## 10. Security requirements checklist

Codex should keep this checklist visible while implementing.

### Must have

- [ ] Private keys are never written under the shared transport path.
- [ ] Provider credentials in `~/.config/observational-memory/env` are never synced.
- [ ] Record payloads are encrypted before transport.
- [ ] Records are signed by node keys.
- [ ] Tampered records are rejected.
- [ ] Unknown-node records are rejected by default.
- [ ] Revoked-node future records are rejected after revocation exists.
- [ ] Materialization ignores tombstoned records.
- [ ] Search index is rebuilt after redaction.
- [ ] Startup hook output is never corrupted by sync logs.

### Should have

- [ ] Raw transcript paths are not exposed in clear metadata.
- [ ] Cluster config and key files have safe permissions.
- [ ] Sync errors are diagnosable.
- [ ] Transport outage does not break local observe/context.
- [ ] Users can inspect provenance.

### Non-goals for v1

- Hosted cloud service.
- Perfect erasure from already-synced compromised machines.
- Multi-user ACLs beyond node membership and namespace tags.
- Full CRDT editing of every memory document.
- Direct P2P as a required dependency.

---

## 11. Reliability and failure-mode tests

Add these as the implementation matures.

### 11.1 Corrupt transport file

- Place invalid JSON in shared transport records directory.
- Sync should reject it, report diagnostic, and continue.

### 11.2 Interrupted local write

- Simulate temp file left behind.
- Store should ignore temp file and not treat it as record.

### 11.3 Duplicate records

- Same record appears in local and transport.
- Sync should not duplicate materialized output.

### 11.4 Clock skew

- Node A clock is one hour ahead.
- Node B clock is one hour behind.
- HLC ordering remains deterministic; no records are dropped.

### 11.5 Concurrent reflections

- Node A and B produce snapshots from different frontiers.
- Sync identifies incomparable snapshots.
- Next reflector run consolidates them.

### 11.6 Redaction propagation

- Node A redacts a record.
- Node B syncs.
- Node B materialized Markdown and search no longer include the content.

### 11.7 Transport outage

- Shared directory missing/unwritable.
- `om observe` still writes local record and materialized memory.
- `om context` still returns valid local context.

### 11.8 Unauthorized node

- Unknown node writes syntactically valid encrypted/signed-looking record.
- Local sync rejects it because node is not a member.

---

## 12. Performance targets

These are targets, not hard guarantees.

| Operation                                  |                                                         Target |
| ------------------------------------------ | -------------------------------------------------------------: |
| `om context` startup sync deadline         |                                                1500 ms default |
| Manual `om cluster sync` for small cluster |                                         < 2 s local filesystem |
| Materialize 1,000 observation records      |                                         < 1 s on modern laptop |
| Record verification/import                 |                                              O(records pulled) |
| Repeated no-op sync                        | Near O(number of heads), not O(all record payload decryptions) |

Implementation suggestions:

- Cache record envelope metadata so no-op sync need not decrypt every payload.
- Decrypt payloads only during materialization or validation requiring payload hash check.
- Keep heads small and cheap to compare.
- Store records by node and sequence so missing ranges are easy to identify.

---

## 13. Documentation updates

Add README sections after Phase 4 or Phase 5, not before the feature is usable.

Suggested sections:

```text
Syncing memory across machines
  Quick start with filesystem transport
  Using Syncthing or cloud folders
  What syncs and what does not
  Security model
  Redaction caveats
  Troubleshooting
```

Example docs:

```bash
# On first machine
om cluster init --name "Bryan Personal" --transport filesystem:~/Sync/om-cluster --import-existing

# On second machine
om cluster join <invite-token>
om cluster sync

# Check state
om cluster status
```

Make this explicit:

```text
Do not sync the whole ~/.local/share/observational-memory directory with a file sync tool.
Use OM Cluster's shared transport directory instead.
```

---

## 14. PR sequencing recommendation

Recommended PR order:

1. **PR 0:** Feature flag, config path helpers, isolated tests.
2. **PR 1:** Record model, crypto, HLC, local store.
3. **PR 2:** Cluster init/status/materialize CLI.
4. **PR 3:** Cluster-aware observe path.
5. **PR 4:** Filesystem transport and manual sync.
6. **PR 5:** Reflection snapshots and frontier merge.
7. **PR 6:** Namespaces, provenance, tombstones, profile overrides.
8. **PR 7:** Automatic cadence, startup pull, diagnostics.
9. **PR 8:** Invite/join, revocation, key rotation.
10. **PR 9:** Optional mDNS/P2P discovery.

Do not start PR 8/9 before PR 4 is stable. Do not start reflection snapshot merge before the two-node filesystem sync test passes.

---

## 15. Codex implementation guardrails

Codex should follow these rules while implementing:

1. Preserve old behavior when cluster is disabled.
2. Keep sync code in `src/observational_memory/sync/` as much as possible.
3. Write tests before or alongside each new persistence behavior.
4. Never store secrets under the shared transport path.
5. Never sync `.cursor.json`, `.search-index`, `.scheduler-logs`, `.qmd-docs`, or provider env files.
6. Never line-merge `reflections.md` or `profile.md` conflicts.
7. Use append-only records and materializers.
8. Make all writes atomic.
9. Treat transport as untrusted.
10. Keep direct P2P and discovery optional.
11. Do not let sync failures prevent local memory capture.
12. Do not print logs to stdout from hook-sensitive commands that must emit JSON.
13. Run `pytest` and `ruff` after each phase.
14. Add `--json` to status/sync commands where automation will consume output.
15. Document every security caveat plainly.

---

## 16. Definition of done for OM Cluster v1

OM Cluster v1 is done when:

- A user can initialize a cluster on one machine.
- A user can add a second machine securely.
- Both machines can observe memories independently.
- Filesystem sync causes both machines to converge.
- `observations.md`, `reflections.md`, `profile.md`, and `active.md` are regenerated locally.
- Search works after sync.
- Reflections use snapshot/frontier semantics, not line merges.
- Memories are encrypted and signed before leaving the machine.
- Unknown/tampered/revoked records are rejected.
- Redactions propagate through tombstones.
- Sync is automatic enough that agents usually see recent remote memories at session start.
- Existing non-cluster OM users are unaffected.
