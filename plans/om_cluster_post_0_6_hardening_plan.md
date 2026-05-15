# OM Cluster Post-0.6 Hardening Plan

Status: implemented hardening plan for issues #44-#47 after the 0.6.0 release and dogfood validation.

## Context

Observational Memory 0.6.0 shipped the first opt-in OM Cluster implementation: encrypted and signed append-only records, request-mode joins, filesystem/relay/P2P transport clients, key epochs, historical rewrap records, redaction tombstones, namespace/source-policy commands, override semantics, record indexing, reflection metadata, stale snapshot pruning, and host-memory coexistence docs.

Post-release validation proved the main path works across real machines:

- A local install initialized a personal cluster with `--import-existing`.
- Three remote installs joined through request-mode approval.
- All four nodes converged through a relay transport.
- Each node wrote and read validation overrides from the other nodes.
- A relay artifact scan did not find plaintext validation values, provider API key strings, private key material, request secrets, or `data_keys`.

This plan captures remaining gaps before the next long-running implementation goal. It intentionally separates public-safe product work from local operator details.

## Issue #41 Resolution Status

Issue #41 asked for per-entry reflection metadata, stale snapshot handling, host-memory coexistence documentation, and cluster-aware semantic merge behavior.

Resolved in 0.6.0:

- Reflection entries get inline `<!--om: ...-->` metadata with stable IDs, kind, `last_seen`, node, and scope.
- `om prune` and reflection post-processing can move or drop stale snapshot entries.
- Host-memory coexistence is documented.
- Cluster materialization uses reflection snapshots, frontiers, and metadata-informed merge guidance.

Remaining from #41 and the agent comments:

- Workstream 6 remains future work: write-side review/feedback loops for end-of-session memory deltas.

Implemented in this hardening pass:

- `om context` now emits a deterministic, budgeted startup payload with cwd/task/agent routing metadata, overflow handles, and recall hints.
- `om recall` expands startup handles and exposes search-backed mid-session retrieval for CLI and plugin use.
- Reflection metadata now supports richer fields (`source_type`, `confidence`, `sensitivity`, `actionability`, `last_verified`, `expires`, `seen_count`) and a `mode` kind while preserving unknown fields.
- Legacy metadata migration can use source file mtime as a better `last_seen` signal when invoked from file-based pruning.
- Generated profile sections can be narrowed with `OM_PROFILE_INCLUDE_IDENTITY=0` or `OM_PROFILE_SECTIONS=...`.
- `scope=local` reflection entries are stripped from shared cluster reflection snapshots and hidden when remote host-local entries are encountered.
- Non-snapshot conflicts are written to `clusters/<cluster-id>/review/reflection-conflicts.{json,md}` and surfaced by `om cluster status --json`.
- A supported stdlib relay server is packaged as `om-relay` and `om cluster relay serve`, with `om cluster relay health` and artifact secrecy scanning.
- `om cluster status --json` now includes transport diagnostics, review artifacts, remediation text, and pending-peer cleanup for approved peers.
- Public-safe repeatable validation guidance lives in `docs/om-cluster-validation.md`.

## Workstream 1 - Startup Context Budgeting And Recall

Problem:

`om context` still emits the generated `profile.md` plus `active.md` as one startup payload. The generated files are more compact than full reflections, but there is no hard budget, task/cwd routing, or expansion protocol. The agent comments in #41 explicitly called out startup payload truncation and mid-session recall as current reliability risks.

Goals:

- Add a size-budgeted startup pack with deterministic truncation behavior.
- Add task/cwd-aware routing, for example `om context --cwd ... --task ... --for codex`.
- Emit clear overflow hints and expansion handles instead of silently relying on a host to preserve large context.
- Add a first-class recall surface for mid-session retrieval, either CLI-first or plugin/skill-friendly.

Acceptance criteria:

- Done: `om context` never emits unbounded startup text by default.
- Done: Tests cover budget boundaries, overflow summaries, and generated expansion handles.
- Done: `om recall` keeps search/retrieval available for deeper memory without loading everything at session start.
- Done: Documentation explains compact startup, expansion, and recall behavior for Codex, Claude Code, Cowork, and Hermes.

## Workstream 2 - Richer Memory Metadata And Conflict Surfacing

Problem:

The 0.6.0 metadata schema is intentionally small. It distinguishes broad kinds and freshness, but it does not yet encode the operational force or trust of a memory. That makes clustered merge safer than before, but still too likely to smooth over disagreements.

Goals:

- Extend metadata with fields such as `source_type`, `confidence`, `sensitivity`, `actionability`, `last_verified`, `expires`, `seen_count`, and a distinct `mode` kind.
- Preserve unknown fields and support legacy migration without damaging manual edits.
- Make conflicts explicit for policies, preferences, identity, and high-actionability entries.
- Keep snapshot-style last-writer behavior for low-risk state facts while surfacing non-snapshot conflicts for review.

Acceptance criteria:

- Done: Metadata round-trip tests cover old and new fields plus unknown-field preservation.
- Done: Snapshot, evergreen, preference, policy, identity, task, decision, and mode semantics are documented in `docs/coexistence.md`.
- Done: Conflicting non-snapshot entries produce inspectable conflict artifacts and status items.
- Done: Tests cover conflicting reflection snapshots and verify operator-visible conflict output.

## Workstream 3 - Host-Memory Controls

Problem:

The coexistence documentation says which system should own which memory type, but users cannot yet enforce the boundary precisely. In particular, there is no documented config switch for narrowing OM profile generation when a host-agent memory system should own identity or host-local behavior.

Goals:

- Add config/env controls for generated profile sections, such as disabling identity/profile generation or marking sections host-local.
- Honor `scope=local` consistently in materialization and cluster sharing.
- Improve docs with concrete recipes for host-agent memory plus OM.

Acceptance criteria:

- Done: Users can disable or narrow profile/identity generation without disabling observations, reflections, search, recall, or cluster sync.
- Done: Host-local entries are filtered before shared cluster reflection snapshots and hidden if encountered from remote nodes.
- Done: Docs include conflict precedence and practical setup examples.

## Workstream 4 - Production Relay And Transport Operations

Problem:

0.6.0 includes relay and P2P clients plus fixture-grade relay validation. It does not yet package a production relay server or service unit. Dogfood validation used a small file-backed relay and a local tunnel, which proved protocol behavior but should not become an undocumented operational dependency.

Goals:

- Package a low-dependency relay server command or optional extra.
- Add systemd/launchd/container examples for running the relay.
- Add relay health/status checks, retention guidance, and safe backup/cleanup procedures.
- Document access control clearly: relay access can prevent abuse, but cluster membership remains local cryptographic trust.

Acceptance criteria:

- Done: Supported commands can run the relay without copying a fixture script: `om-relay` and `om cluster relay serve`.
- Done: Operators can install, start, stop, inspect, and upgrade the relay predictably with documented systemd/launchd examples.
- Done: Relay health checks verify storage, endpoint version, and no obvious plaintext/private-key artifacts.
- Done: Tests exercise relay outage/malformed data and preserve local-first behavior when relay is down.

## Workstream 5 - Cluster Diagnostics And UX Polish

Problem:

Production validation found a successful-but-confusing diagnostic state: remote nodes still reported approved peers in `pending_peers` after all four nodes were trusted and converged. The system worked, but the status output invites doubt.

Goals:

- Clean up pending-peer diagnostics after approval/membership import.
- Improve `om cluster status --json` with explicit request states, stale pending metadata, relay reachability, and per-transport last error.
- Add clearer remediation text for pending approval, expired invites, unreachable relay, revoked peer, and missing local keys.

Acceptance criteria:

- Done: Approved peers no longer appear as pending in normal converged status.
- Done: Tests cover pending-to-approved cleanup for filesystem and relay transports.
- Done: Status output distinguishes pending peers, join request state, review artifacts, and transport reachability.
- Done: CLI messages remain safe for public logs and do not print secrets.

## Workstream 6 - Write-Side Review And Agent Feedback

Problem:

Agents can search memory and run dry-run reflection/pruning commands, but there is no end-of-session view of what would be promoted, changed, or removed before memory hardens into future startup context.

Goals:

- Add a checkpoint dry-run or review command that shows pending observations, candidate reflection edits, stale entries, promotions, and removals.
- Add an agent feedback path for marking memory useful, stale, noisy, unsafe, misleading, or missing.
- Feed feedback into reflection metadata and future startup ranking.

Acceptance criteria:

- A user or agent can inspect the memory delta before committing it.
- Feedback records are structured and searchable.
- Tests cover dry-run output stability and feedback preservation.

## Workstream 7 - Validation Matrix

Problem:

The release had strong unit, fixture, temp-home, and dogfood validation, but the process should be repeatable without reconstructing local operator knowledge.

Goals:

- Add a public-safe validation checklist for multi-node temp installs.
- Add a separate private/local skill for machine-specific dogfood commands.
- Add CI or scripted coverage for relay/P2P convergence, request approval, redaction, key epochs, rewrap, pending-peer cleanup, and transport secrecy.
- Preserve honest gaps for real Windows validation and relay productionization.

Acceptance criteria:

- Done: Public docs describe what to validate without exposing private hostnames, IPs, or operator paths.
- Deferred/local: machine-specific dogfood commands belong in a local-only skill outside tracked files.
- Done: A fresh session can run the validation path from the checklist and produce comparable evidence.

## Suggested Issue Split

Tracking issues:

1. [#44](https://github.com/intertwine/observational-memory/issues/44) - Startup context budget and first-class recall.
2. [#45](https://github.com/intertwine/observational-memory/issues/45) - Richer metadata, host-memory controls, and conflict surfacing.
3. [#46](https://github.com/intertwine/observational-memory/issues/46) - Production relay packaging and transport operations.
4. [#47](https://github.com/intertwine/observational-memory/issues/47) - Cluster diagnostics, pending-peer cleanup, and repeatable validation.

These issues can be implemented independently but should be validated together before a 0.6.x hardening release.
