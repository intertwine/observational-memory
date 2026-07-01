# Release Notes - v0.9.0

## Theme

v0.8.0 made memory trustworthy. v0.9.0 makes it broader and safer to leave running in the background.

This release adds first-class OpenCode and Kimi Code CLI support, and it hardens the background observer workers that installed hooks and schedulers use. The result is more host coverage without letting local observation pile up unbounded Python workers under load.

Everything remains local-first. Existing Claude Code, Codex, Grok Build TUI, Cowork, Hermes, OM Cluster, and OM Mail workflows continue to work. OM Mail remains experimental in this release; handshake tokens, live listening, digests, group addresses, and team trust roots are deferred to a future 0.x release.

## OpenCode joins the shared memory layer

Install:

```bash
om install --opencode
```

OpenCode support adds:

- a global plugin in `~/.config/opencode/plugins/`;
- a global `AGENTS.md` fallback for bounded startup context;
- OM-owned JSONL event logs under the memory directory;
- `om observe --source opencode`;
- `om context --for opencode`;
- `om status` and `om doctor` checks for the plugin, fallback, and event logs.

The plugin records stable message events. The parser is defensive because OpenCode event shapes can change; unknown events and non-message lifecycle records are ignored.

## Kimi Code CLI joins the shared memory layer

Install:

```bash
om install --kimi
```

Kimi support adds:

- managed hooks in `~/.kimi/config.toml`;
- `KIMI_HOME` support for alternate Kimi homes;
- `om context --for kimi`;
- `om kimi-checkpoint` for safe hook-event capture;
- `om observe --source kimi`;
- `OM_KIMI_OBSERVER_INTERVAL_SECONDS` to throttle checkpoint-triggered observation.

Kimi does not expose a full Claude-style transcript today, so OM observes lifecycle events instead of scraping private provider data: user prompts, subagent starts, subagent stops, and stop failures.

## Background observation is bounded

Installed hook and scheduler jobs now route background observation through:

```bash
om observe-worker --source codex
```

The hidden worker lane allows one background observer at a time. If another observer is already running, follow-up workers exit cleanly instead of starting another LLM-heavy pass. If a worker exceeds the timeout, it is stopped and the lock is released.

Defaults:

```bash
OM_OBSERVER_WORKER_TIMEOUT_SECONDS=300
OM_OBSERVER_WORKER_LOCK_STALE_SECONDS=360
```

POSIX workers use `SIGALRM`. Windows workers run observer work in a child process that the parent terminates or kills on timeout. Manual `om observe ...` commands remain explicit operator runs and are not forced through the background lane.

The hardening also includes:

- dead-owner PID lock reclaim shared across lock implementations;
- checkpoint locks stamped with the long-running worker PID, not only the short-lived spawner;
- live worker locks protected from age-only stealing;
- atomic writes for observations and cursor state;
- fail-soft cursor loading for corrupt JSON;
- a scheduled Claude transcript backstop matching the Codex backstop.

## Public roadmap additions

v0.9.0 also adds public plans for:

- optional Open Knowledge Format export/import as a reviewed interchange path;
- signed standalone `om` binaries and a thin desktop control center;
- a future desktop "middle manager" layer for memory-backed agent coordination.

These are plans, not shipped runtime features in v0.9.0.

## Aside browser work in progress

Aside browser integration is active in draft PR [#98](https://github.com/intertwine/observational-memory/pull/98). It is not part of v0.9.0. The draft is exploring first-class Aside transcript parsing, `om observe --source aside`, and an Aside-side OM skill.

## Plugin ecosystem

The v0.9.0 release train is coordinated with compatibility updates for the external Hermes memory-provider plugin and the Grok plugin. Core `om` keeps Hermes transcript ingestion in this repository, but live Hermes memory-provider behavior is validated and released from `intertwine/hermes-observational-memory`. The Grok plugin remains a separate marketplace/plugin package under review.

## Compatibility

- Existing installs do not need config changes.
- `om install --all` now includes OpenCode and Kimi alongside the existing supported hosts.
- Background observer jobs become more conservative: busy workers skip instead of multiplying.
- OM Mail remains experimental, CLI-only, opt-in, and off unless configured.

## Validation

- Main CI is green at the release-prep head before tagging.
- The bounded-worker PR passed full suite validation during review (`1092 passed`) plus focused observer/install/Windows tests.
- OpenCode and Kimi PRs each passed full-suite validation during review.

## Out of scope / next

- OM Mail GA remains on the future 0.x roadmap: handshake tokens, live listening, digests, group addresses, and team trust roots.
- Aside browser support remains in draft PR #98.
- OKF support is planned as optional export/import, not native storage.
- Binary and desktop installers are planned, not shipped in this release.
