# Host Memory Coexistence

Observational Memory can run beside host-agent memory systems such as Claude Code memory, Codex startup context, ChatGPT memory, or other product-local recall layers.

Recommended ownership:

- Host memory owns host-local feedback, UI/tool preferences, ephemeral conversation state, and behavior that applies only inside that host.
- Observational Memory owns cross-agent project state, multi-host identity/preferences, durable workflow rules, architectural memory, and memory intended to sync across machines.

Overlap is acceptable, but conflict resolution should be explicit:

- Host-local rules win for host-specific behavior.
- OM wins for cross-agent and cross-machine project memory.
- Manual OM overrides win over generated reflection entries.
- Snapshot facts should decay unless refreshed by new observations.

Reflection entries now carry inline metadata comments:

```markdown
- PR #33 is open <!--om: id=ome_abc kind=snapshot actionability=low last_seen=2026-05-14T12:00:00Z node=node_a scope=cluster-->
- Use worktrees for risky branches <!--om: id=ome_def kind=evergreen actionability=medium last_seen=2026-05-14T12:00:00Z node=node_a scope=cluster-->
- Codex-only UI preference <!--om: id=ome_local kind=preference scope=local node=node_a-->
```

Snapshot entries age according to `OM_SNAPSHOT_TTL_DAYS` (default `14`) and `OM_SNAPSHOT_EXPIRY_ACTION` (default `stale-section`). Run `om prune` to apply pruning without waiting for the next reflection pass.

Supported metadata keys are open-ended and unknown fields are preserved. Current generated keys include:

- `kind`: `snapshot`, `evergreen`, `preference`, `policy`, `identity`, `task`, `decision`, or `mode`.
- `source_type`: usually `inferred` for reflector-generated metadata.
- `confidence`: current confidence, defaulting to `medium`.
- `sensitivity`: `normal` or `personal` for identity/policy-style entries.
- `actionability`: `low`, `medium`, or `high`; high-actionability conflicts are reviewable.
- `last_seen`, `last_verified`, and `expires`: freshness and verification timestamps when known.
- `seen_count`: reinforcement count, defaulting to `1`.
- `scope`: `cluster` for shared memory or `local` for host-local materialization.

Generated profile controls:

```bash
# Do not include generated identity in profile.md/startup context.
OM_PROFILE_INCLUDE_IDENTITY=0 om context

# Only include selected generated profile sections.
OM_PROFILE_SECTIONS=preferences,relationship,key-facts om context
```

These controls narrow generated startup/profile materialization only. They do not disable observation capture, reflection, search, recall, or cluster sync.

Cluster behavior for host-local scope:

- `scope=local` reflection entries are stripped before shared cluster reflection snapshots are appended.
- Remote `scope=local` entries are hidden during materialization if an older record already carried them.
- Use host memory for behavior that should apply only inside one product or machine; use `scope=cluster` for cross-agent rules.

Conflict surfacing:

- Snapshot facts remain low-risk last-writer/current-state material.
- Disagreements among policy, preference, identity, decision, mode, or high-actionability entries are written to cluster review artifacts.
- `om cluster status --json` reports review artifact counts and remediation text without printing secrets.
