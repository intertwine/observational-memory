# Make Observational Memory A First-Class Hermes Plugin

Status: tracked in https://github.com/intertwine/observational-memory/issues/42. Implementation is now on the standalone plugin path against the current `observational-memory` `0.6.3` release line.

Source: local Codex research on Bryan Young's machine, May 12-13, 2026. This captures learnings from the paused Hermes native-provider PR and Discord advocacy effort.

## Goal

Make Observational Memory generally available to Hermes users as a first-class external memory-provider plugin.

The plugin should let Hermes join the same local-first memory fabric as Claude Code, Codex, Grok Build TUI, and Claude Cowork:

- compact startup context from OM `profile.md` and `active.md`
- on-demand recall through `om_context` and `om_search`
- explicit durable writes through `om_remember`
- optional Hermes session writeback into OM observations
- OM Cluster pull-before-context so Hermes can see memory shared across machines

## Learnings From The Hermes PR

- PR `NousResearch/hermes-agent#12583` proved the provider surface is feasible, but it is a noisy vehicle for adoption because Hermes main had unrelated full-suite failures and a fork-permission lint-comment failure.
- The reviewer guidance was to discuss new plugin support in the Hermes developer/plugin channel rather than treating native inclusion as purely a code-review question.
- Hermes now has user-installed memory-provider discovery from `$HERMES_HOME/plugins/<name>`, so the old standalone-plugin symlink workaround is stale for recent Hermes builds.
- Hermes memory providers are `kind: exclusive`; they are activated through `memory.provider`, not `plugins.enabled`.
- A standalone plugin can be loaded through the current Hermes memory-provider discovery path. A temp Hermes home discovered and loaded the existing `intertwine/hermes-observational-memory` provider; it only reported unavailable when the `observational-memory` Python package was absent from that Hermes environment.
- PR `NousResearch/hermes-agent#12583` is closed. The maintainer direction is standalone plugin first, with upstream Hermes docs as the right follow-up once the plugin is public and validated.

## Current Assets

- Native Hermes PR: `https://github.com/NousResearch/hermes-agent/pull/12583` (closed)
- Standalone plugin repo: `https://github.com/intertwine/hermes-observational-memory`
- OM package repo: `https://github.com/intertwine/observational-memory`
- Hermes user memory-provider discovery was fixed in `NousResearch/hermes-agent#10529` and is present by tag `v2026.4.16`.
- Current OM package release: `0.6.3`.

## Implementation Outline

1. Use the current `observational-memory` `0.6.3` release line.
   - Keep OM Cluster opt-in and disabled unless initialized.
   - Preserve the local Markdown materialized-view model.
   - Do not revive older `0.6.0` issue text as the dependency target.

2. Refresh `intertwine/hermes-observational-memory`.
   - Port the current provider implementation and tests from the Hermes PR.
   - Keep the plugin as a Hermes memory provider, not a broad Hermes core patch.
   - Add explicit `kind: exclusive` to `plugin.yaml`.
   - Update dependency metadata to `observational-memory>=0.6.3,<0.7`.
   - Remove the symlink install workaround from docs for supported Hermes versions.

3. Add first-class setup and validation docs.
   - Install: `hermes plugins install intertwine/hermes-observational-memory --no-enable`.
   - Configure: `hermes memory setup`, select `observational_memory`.
   - Verify: `hermes memory status`, `om doctor`, and a short read/write/search smoke path.
   - Document required Hermes version or fallback instructions for older builds.

4. Add OM Cluster integration.
   - If OM Cluster is enabled and `sync_before_context` is true, the plugin should call `sync_cluster(..., pull_only=True)` before reading startup context.
   - Treat this as optional and best-effort so older OM versions or disabled clusters keep working.

5. Produce a proof artifact.
   - Fresh Hermes profile or temp `HERMES_HOME`.
   - Install plugin from GitHub.
   - Install `observational-memory`.
   - Select the provider through `hermes memory setup`.
   - Show `om_context`, `om_search`, `om_remember`, and session writeback working.
   - Include a two-machine or two-temp-home cluster smoke proof.

6. Complete public release and bookkeeping.
   - Publish a plugin release.
   - Validate the released plugin on real Hermes instances.
   - Close the OM tracking issue with release and validation evidence.

## Acceptance Criteria

- `intertwine/hermes-observational-memory` has current provider code, tests, and docs.
- A supported Hermes release can install the plugin without symlinking into the Hermes source tree.
- `hermes memory setup` discovers `observational_memory` from `$HERMES_HOME/plugins`.
- The plugin can load startup context, search OM memory, remember explicit notes, and write back session turns.
- The plugin degrades gracefully when OM Cluster is absent or disabled.
- Docs explain how Hermes participates in cross-machine memory through OM Cluster.
- The Hermes native-provider PR is closed and the OM tracking issue points to the released plugin path.
