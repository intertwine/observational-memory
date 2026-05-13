# Make Observational Memory A First-Class Hermes Plugin

Status: tracked in https://github.com/intertwine/observational-memory/issues/42, planned for after the `0.6.0` OM Cluster release.

Source: local Codex research on Bryan Young's machine, May 12-13, 2026. This captures learnings from the paused Hermes native-provider PR and Discord advocacy effort.

## Goal

Make Observational Memory generally available to Hermes users as a first-class external memory-provider plugin, then close the native-provider PR in `NousResearch/hermes-agent`.

The plugin should let Hermes join the same local-first memory fabric as Claude Code, Codex, and Claude Cowork:

- compact startup context from OM `profile.md` and `active.md`
- on-demand recall through `om_context` and `om_search`
- explicit durable writes through `om_remember`
- optional Hermes session writeback into OM observations
- post-`0.6.0` optional OM Cluster pull-before-context so Hermes can see memory shared across machines

## Learnings From The Hermes PR

- PR `NousResearch/hermes-agent#12583` proved the provider surface is feasible, but it is a noisy vehicle for adoption because Hermes main had unrelated full-suite failures and a fork-permission lint-comment failure.
- The reviewer guidance was to discuss new plugin support in the Hermes developer/plugin channel rather than treating native inclusion as purely a code-review question.
- Hermes now has user-installed memory-provider discovery from `$HERMES_HOME/plugins/<name>`, so the old standalone-plugin symlink workaround is stale for recent Hermes builds.
- A standalone plugin can be loaded through the current Hermes memory-provider discovery path. A temp Hermes home discovered and loaded the existing `intertwine/hermes-observational-memory` provider; it only reported unavailable when the `observational-memory` Python package was absent from that Hermes environment.
- Focused standalone plugin tests in `intertwine/hermes-observational-memory` pass today, but the repo lags the native PR and the OM package feature set.

## Current Assets

- Native Hermes PR: `https://github.com/NousResearch/hermes-agent/pull/12583`
- Standalone plugin repo: `https://github.com/intertwine/hermes-observational-memory`
- OM package repo: `https://github.com/intertwine/observational-memory`
- Hermes user memory-provider discovery was fixed in `NousResearch/hermes-agent#10529` and is present by tag `v2026.4.16`.

## Implementation Outline

1. Finish and release `observational-memory` `0.6.0`.
   - Keep OM Cluster opt-in and disabled unless initialized.
   - Preserve the local Markdown materialized-view model.
   - Carry forward `0.5.7` Windows compatibility.

2. Refresh `intertwine/hermes-observational-memory`.
   - Port the current provider implementation and tests from the Hermes PR.
   - Keep the plugin as a Hermes memory provider, not a broad Hermes core patch.
   - Add explicit `kind: exclusive` to `plugin.yaml`.
   - Update dependency metadata to a post-`0.6.0` range, likely `observational-memory>=0.6.0,<0.7`.
   - Remove the symlink install workaround from docs for supported Hermes versions.

3. Add first-class setup and validation docs.
   - Install: `hermes plugins install intertwine/hermes-observational-memory`.
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
   - If `0.6.0` is available, include a two-machine or two-temp-home cluster smoke proof.

6. Close the native Hermes PR.
   - Comment on PR `#12583` that the work moved to the standalone plugin path.
   - Link the plugin repo, the OM tracking issue, and the validation artifact.
   - Close the PR once the plugin is generally available.

## Acceptance Criteria

- `intertwine/hermes-observational-memory` has current provider code, tests, and docs.
- A supported Hermes release can install the plugin without symlinking into the Hermes source tree.
- `hermes memory setup` discovers `observational_memory` from `$HERMES_HOME/plugins`.
- The plugin can load startup context, search OM memory, remember explicit notes, and write back session turns.
- The plugin degrades gracefully when OM Cluster is absent or disabled.
- Post-`0.6.0` docs explain how Hermes participates in cross-machine memory through OM Cluster.
- The Hermes native-provider PR is closed with a clear pointer to the plugin path.
