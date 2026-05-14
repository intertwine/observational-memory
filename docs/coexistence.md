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
- PR #33 is open <!--om: id=ome_abc kind=snapshot last_seen=2026-05-14T12:00:00Z node=node_a scope=cluster-->
- Use worktrees for risky branches <!--om: id=ome_def kind=evergreen last_seen=2026-05-14T12:00:00Z node=node_a scope=cluster-->
```

Snapshot entries age according to `OM_SNAPSHOT_TTL_DAYS` (default `14`) and `OM_SNAPSHOT_EXPIRY_ACTION` (default `stale-section`). Run `om prune` to apply pruning without waiting for the next reflection pass.
