# Binary Desktop Installer Plan

Status: PROPOSED
Related docs: `docs/install.md`, `docs/configuration.md`, `docs/search-and-recall.md`, `docs/om-cluster-sync.md`, `docs/mail-memory.md`, `docs/talk-to-memories.md`

## Goal

Make Observational Memory easy to install and manage without requiring a user to know Homebrew, `uv`, Python, or a shell.

The outcome is a signed desktop app plus signed command-line binaries for every supported platform:

- macOS Apple silicon and Intel.
- Windows x64 and ARM64.
- Linux x64 and ARM64.

The desktop app should be a thin, local-first control center for the existing `om` engine. It should install or update the `om` binary, run health checks, open the relevant memory files, offer a chat-style `om talk` UI, and manage OM Cluster and OM Mail without hiding the trust model.

## Non-goals

- Do not replace the CLI. The app is a wrapper and manager for `om`, not a fork.
- Do not add hosted memory storage.
- Do not sync the live memory directory through a desktop cloud folder.
- Do not silently write ChatGPT or Claude Managed Agents memory.
- Do not make OM Cluster or OM Mail enabled by default.
- Do not bump versions, tag releases, publish packages, or update Homebrew as part of this plan.

## Product shape

Ship three layers that share one release pipeline.

1. **Standalone `om` binaries.** A single-file executable per target platform. These support users who still want the terminal but do not want Python tooling.
2. **Desktop app.** A small app called **Observational Memory** that bundles or downloads the matching `om` binary and exposes common workflows.
3. **Optional background helper.** A user-level helper for scheduled reflect, mail sync, and update checks. It must be visible, disable-able, and never required for normal reads.

The app should treat the local Markdown files as first-class. Users should always be able to see and edit their memory in their normal editor.

## Recommended implementation stack

### Binary packaging

Use **PyApp** or **PyOxidizer** only if they can pass the integration test matrix with the current dependencies. Otherwise use **Nuitka standalone** as the first fallback.

Required properties:

- No Python install required on the target machine.
- Reproducible builds from locked dependencies.
- `om --version`, `om doctor`, `om context`, `om recall`, `om talk`, `om cluster`, and `om mail` work from the packaged binary.
- The binary can locate and write the same data and config paths documented for the Python install.
- The binary can be code signed and notarized where the platform expects it.

### Desktop app

Use **Tauri v2** for the first implementation.

Reasons:

- Good fit for a small local UI that mostly shells out to an existing binary.
- Native installers are available for macOS, Windows, and Linux.
- The Rust side can own process management, file opening, update checks, and OS integration.
- The web UI can stay simple and testable.

Avoid Electron unless Tauri blocks a required feature. The app should not feel larger than the memory tool it manages.

## Release artifacts

Each release should produce:

| Platform | CLI artifact | Desktop artifact | Signing target |
| --- | --- | --- | --- |
| macOS arm64 | `om-aarch64-apple-darwin.tar.gz` | `.dmg` and `.app.tar.gz` | Developer ID + notarization |
| macOS x64 | `om-x86_64-apple-darwin.tar.gz` | `.dmg` and `.app.tar.gz` | Developer ID + notarization |
| Windows x64 | `om-x86_64-pc-windows-msvc.zip` | `.msi` or `.exe` | Authenticode |
| Windows ARM64 | `om-aarch64-pc-windows-msvc.zip` | `.msi` or `.exe` | Authenticode |
| Linux x64 | `om-x86_64-unknown-linux-gnu.tar.gz` | `.AppImage`, `.deb`, `.rpm` | Checksums, optional cosign |
| Linux ARM64 | `om-aarch64-unknown-linux-gnu.tar.gz` | `.AppImage`, `.deb`, `.rpm` | Checksums, optional cosign |

Also publish `SHA256SUMS` and signed provenance for every artifact.

## Desktop UI requirements

### 1. Welcome and install

The first run should show:

- What OM does in plain English.
- Where memory and config will live.
- A button to install or repair the `om` binary.
- A provider setup path that reuses `om login` or the current provider config flow.
- A final **Run Doctor** step.

The UI should show the equivalent CLI command for every action in a collapsible details area. This keeps the product teachable without making the command line mandatory.

### 2. Doctor dashboard

Expose `om doctor` as the app home screen.

Show:

- Overall status: healthy, needs attention, or broken.
- Provider status.
- Hook install status for Claude Code, Codex, Grok, Hermes, and Cowork where available.
- Search index status and a **Reindex** button.
- Backup status and latest snapshot.
- Memory growth and quality warnings.
- Cluster status if configured.
- Mail status if configured.

Implementation requirement: add or stabilize `om doctor --json` if the existing structured output is incomplete. The desktop app should not scrape human text.

### 3. Open memory files

Provide buttons for:

- Open memory folder.
- Open config folder.
- Open `profile.md`.
- Open `active.md`.
- Open `reflections.md`.
- Open `observations.md`.
- Open backups folder.
- Open mail inbox state when OM Mail is configured.

The app should use the OS default file manager or editor. It should not build a full Markdown editor in the first version.

### 4. Chat-style `om talk`

Provide a chat panel backed by `om talk` and recall.

Minimum behavior:

- User sends a message.
- App streams or displays the grounded answer.
- UI shows when recall is unavailable, timed out, or returned no relevant memory.
- UI shows citations or memory snippets when `om talk` can provide them.
- A **Reindex and retry** action appears when the recall backend is unavailable.
- The transcript is local and clearable.

Implementation requirement: add a desktop-friendly protocol such as `om talk --jsonl` or `om talk serve --stdio` so the UI can avoid brittle terminal automation.

### 5. Cluster management

Expose the existing opt-in OM Cluster model without weakening it.

Screens:

- **Status:** current cluster name, node alias, enabled state, transports, last sync, pending requests.
- **Create cluster:** wraps `om cluster init` and explains that this is for trusted personal nodes.
- **Join cluster:** paste invite token and choose node alias.
- **Create invite:** choose normal or request-mode invite, show expiration, copy token with a secret warning.
- **Requests:** approve or reject pending request-mode joins.
- **Sync:** run pull or full sync with visible results.
- **Disable:** turn off cluster without deleting memory.

Safety rules:

- Never suggest syncing the live memory directory with iCloud, Dropbox, Syncthing, rsync, or a NAS.
- Keep relay transport clearly separate from trust. Relay access is not cluster trust.
- Hide no key warnings. Invite tokens that carry key material must look secret.

Implementation requirement: add JSON output for cluster status, invite creation metadata, request lists, and sync results where missing.

### 6. Mail management

Expose OM Mail as experimental and fail-closed.

Screens:

- **Account:** initialize mailbox, show address, provider, and public key.
- **Peers:** add, edit, remove, and verify peers. Make pinned keys visible.
- **Inbox review:** list held messages, accept or reject notes, inspect failed verification reasons.
- **Send note:** send a short memory note to a trusted peer.
- **Send pack:** create and send a scoped context pack only after review.
- **Ask peer:** chat-like recall request to a peer.
- **Sync/respond:** run mail sync and optionally answer allowed recall requests.

Safety rules:

- Unknown sender, bad signature, bad key, failed decrypt, and malformed envelope stay held.
- The UI must not add `auto_accept` or `allow_recall` without an explicit warning.
- Context packs must show scope filtering before send.
- Mail state remains host-local and is not cluster synced.

Implementation requirement: add JSON output for mail account, peer list, held inbox, sync, accept/reject, send-note, send-pack, and ask flows where missing.

## Engineering plan

### Phase 0 — Audit and contracts

Deliverables:

- Inventory every CLI command the desktop app needs.
- Add stable JSON contracts for missing command outputs.
- Document data paths and command behavior for the app.
- Add golden tests for the JSON schemas.

Acceptance:

- The UI team can build against documented command contracts.
- Human CLI output can change without breaking the app.

### Phase 1 — Binary CLI artifacts

Deliverables:

- Build scripts for all target triples.
- Smoke tests that run each packaged binary in a clean temp home.
- Artifact checksums and provenance.
- Signing dry runs in CI, with real signing on release runners.

Acceptance:

- A user can download `om`, put it on PATH, and run `om doctor` without Python or `uv`.

### Phase 2 — Desktop shell

Deliverables:

- Tauri app skeleton.
- First-run installer and repair flow.
- Doctor dashboard using `om doctor --json`.
- File-opening actions.
- Basic settings page showing paths and app version.

Acceptance:

- A non-terminal user can install, validate, and open their memory files.

### Phase 3 — Chat UI

Deliverables:

- `om talk` JSONL or stdio protocol.
- Chat panel with recall status states.
- Local transcript clear action.
- Reindex and retry action.

Acceptance:

- A user can ask memory questions from the desktop app and understand when answers are not grounded.

### Phase 4 — Cluster UI

Deliverables:

- Cluster status, init, invite, join, requests, sync, and disable screens.
- Clear trust warnings.
- JSON contracts and tests for every wrapped cluster command.

Acceptance:

- A user can set up a trusted personal cluster from the UI without reading CLI docs, while still seeing the trust boundary.

### Phase 5 — Mail UI

Deliverables:

- Mail account, peers, inbox review, send note, send pack, ask, and sync screens.
- Fail-closed review states.
- JSON contracts and tests for every wrapped mail command.

Acceptance:

- A user can exchange memory with a pinned peer from the UI without weakening OM Mail's trust model.

### Phase 6 — Updater and polish

Deliverables:

- In-app update checks with signed metadata.
- Download, verify, and install update flow.
- Crash/error reporting that is local by default and exportable as a review bundle.
- Accessibility pass.
- Documentation for desktop install and binary install.

Acceptance:

- Users can keep OM current without Homebrew or `uv`.

## Test plan

Run these on every PR that touches packaging or desktop code:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Add these CI jobs before shipping binaries:

- Build packaged `om` for every target.
- Run packaged binary smoke tests on macOS, Windows, and Linux.
- Run desktop app unit tests.
- Run Tauri build checks for every desktop target.
- Run installer smoke tests in clean VMs.
- Verify signatures, notarization, checksums, and update metadata.

Manual release checklist:

- Fresh macOS install with no Homebrew and no Python.
- Fresh Windows install with no Python.
- Fresh Linux install with no Python.
- Existing `uv` install upgraded to binary app without losing memory.
- Existing Homebrew install coexists with app and warns clearly about PATH precedence.
- Cluster init and join between two machines.
- Mail localdir test and live provider test.
- `om talk` chat with recall available, unavailable, timeout, and no-result cases.

## Adversarial review

### Failure mode 1: packaging creates a second product

Risk: the desktop app reimplements behavior and drifts from the CLI.

Mitigation: the app shells out to `om` or uses a narrow local protocol owned by `om`. The CLI remains the source of truth. All UI features must have CLI equivalents.

### Failure mode 2: binary packaging breaks hidden Python assumptions

Risk: dynamic imports, provider extras, certificates, or native dependencies fail only after release.

Mitigation: package smoke tests must run real commands in clean OS environments. Treat `om doctor`, `om talk`, `om cluster status`, and `om mail` as required smoke paths, not just `--help`.

### Failure mode 3: UI hides trust and makes unsafe sharing easy

Risk: desktop convenience causes users to sync live memory folders, share cluster secrets, or auto-accept mail.

Mitigation: make warnings explicit, keep dangerous actions multi-step, show the equivalent CLI command, and keep cluster/mail opt-in. The UI must repeat that relay access is not trust and that live memory directories must not be cloud-synced.

### Failure mode 4: updater becomes a supply-chain weak point

Risk: a compromised update channel installs a malicious memory tool.

Mitigation: signed update metadata, signed artifacts, checksum verification, release provenance, and no silent major updates. The app should be able to disable update checks.

### Failure mode 5: multiple installs fight each other

Risk: Homebrew, `uv`, app-bundled `om`, and PATH installs point to different versions.

Mitigation: `om doctor --json` should report binary path, version, install kind, and PATH precedence. The app should show which binary it uses and offer repair steps.

### Failure mode 6: chat UI over-promises grounding

Risk: desktop chat feels like a general assistant and users trust ungrounded answers.

Mitigation: show recall status in every answer. If recall is unavailable, timed out, or empty, label the answer clearly. Prefer no answer over a confident ungrounded answer for memory-specific questions.

### Failure mode 7: Linux desktop support becomes too broad

Risk: AppImage, deb, rpm, Wayland/X11, keychains, and file openers create a long tail.

Mitigation: define support tiers. Tier 1 is Ubuntu LTS and Fedora current on x64. ARM64 Linux starts as best effort until CI runners and testers are in place.

### Failure mode 8: background helper surprises users

Risk: scheduled reflect or mail sync runs when users do not expect it.

Mitigation: no background helper on by default except update checks if the user opts in during install. Show helper status on the dashboard and provide one-click disable.

### Failure mode 9: app logs leak private memory

Risk: UI logs command output that includes memory content, peer addresses, or paths.

Mitigation: redact logs by default. Store logs locally. Provide an export flow that previews the bundle before sharing.

### Failure mode 10: scope expands into a full memory editor

Risk: building an editor delays the main adoption path.

Mitigation: first version opens files in the user's editor. Editing, diffing, and review workflows can come later after install and management are solved.

## Recommended first milestone

Start with Phase 0 and Phase 1 only.

The binary CLI artifacts reduce install friction immediately and force the right packaging discipline before any UI exists. Once `om doctor` and the core workflows are reliable in a no-Python install, the desktop app can be a thin and trustworthy layer instead of a workaround for packaging gaps.

---

# Expansion Report — OM Desktop as a Middle-Management Layer

Status: PROPOSED EXTENSION
Primary prompt: Sawyer Hood, "I Was Tired of Juggling My Agents, So I Hired a Middle Manager" (https://www.sawyerhood.com/blog/hired-a-middle-manager)
Comparison sources: Devin by Cognition, Factory, Tembo, OpenHands.

## Executive summary

The desktop installer plan should stay the first adoption milestone, but the app can grow into a more valuable layer: a **local middle manager for agent work**.

The opportunity is not to make OM another coding agent. The opportunity is to make OM the persistent, reviewable coordination layer that sits above Claude Code, Codex, Grok, Hermes, Cowork, Devin-style hosted agents, and future workers. The user talks to one manager. The manager remembers goals, breaks work into tasks, delegates to available agents, tracks work in plain files, serializes merges, runs checks, and keeps the human in control.

This matches OM's strengths:

- OM already stores durable local context.
- OM already integrates with multiple agent harnesses.
- OM already has `om talk`, `om recall`, OM Cluster, and OM Mail.
- OM's Markdown-first posture maps well to the article's "bring your own task management" lesson.
- OM can avoid the fragile-agent-swarm trap by acting as a single coordination point, not a free-for-all multi-agent mesh.

Recommendation: design the desktop app as **two modes**.

1. **Memory Control Center** — the original plan: install, doctor, files, talk, cluster, mail.
2. **Work Control Center** — a future mode: plan, delegate, monitor, review, and merge agent work through a local-first manager.

Do not build Work Control Center first. Build binary installs and stable JSON/process protocols first. But design those protocols so the desktop app can later become the manager instead of being only a settings UI.

## What the Sawyer Hood article teaches

The article describes a practical pattern, not a polished product:

- One persistent manager agent receives messy human intent.
- The manager breaks work into smaller tasks.
- Different workers are chosen for different task shapes.
- Every coding task runs in a separate git worktree.
- A dedicated merge queue serializes integration.
- A reviewer agent can check work before merge.
- Long-running TODOs live in Obsidian so both human and agent can inspect and edit them.
- The workflow is strongest for side projects and small production fixes, not unreviewed critical work.

The durable insight is that agent productivity is no longer limited only by model quality. It is limited by **coordination load**:

- Too many chats.
- Too many branches.
- Too many partial plans.
- Too many merge conflicts.
- Too much human context switching.
- Too little persistent state between sessions.

A desktop OM manager could reduce that load while preserving inspectability.

## Comparison: what other orchestrators are doing

| Product / pattern | What it appears to optimize | Useful lesson for OM | What OM should not copy blindly |
| --- | --- | --- | --- |
| Sawyer Hood's middle manager | Personal orchestration of many local/cloud workers, worktrees, merge queue, Obsidian task files. | The human wants one conversational manager and plain task files, not a dozen agent tabs. | Do not assume vibe-coding quality gates are enough for production. |
| Devin by Cognition | A hosted autonomous software engineer that plans, codes, tests, opens PRs, learns codebase knowledge, and works through Slack/GitHub/IDE/API surfaces. | Treat agents like workers with performance profiles. Keep tasks bounded, require tests, provide feedback, and let the agent build durable knowledge. | Do not centralize OM memory in a hosted agent or make users depend on one vendor. |
| Factory | Agent-native SDLC platform with Droid CLI, desktop app, CI/automation, Enterprise, and Missions for planned multi-step work. | Structured planning before execution matters. Mission Control-style progress views are a good UI pattern. | Do not jump straight to a full SDLC platform before OM has reliable local orchestration primitives. |
| Tembo | Harness-agnostic orchestration across agents, repos, and integrations from Slack/Linear/GitHub/Jira/Sentry. | The orchestration layer can be more important than the agent. Routing, recurring workflows, and integration triggers are valuable. | Do not require enterprise SaaS integrations for the personal/local-first product to be useful. |
| OpenHands | Open-source agent platform with SDK, CLI, local GUI, cloud deployment, integrations, RBAC, and collaboration features. | Separate engine, CLI, local GUI, and cloud surfaces. Local GUI plus REST API is a useful precedent. | Do not turn OM into a general agent runtime when its differentiator is memory and governance. |

## Strategic position for OM

OM should occupy the **memory-backed manager** niche:

```text
Human intent
  -> OM Desktop Manager
      -> durable plan files
      -> task queue
      -> local memory / recall
      -> worker harness adapters
      -> worktree isolation
      -> merge queue
      -> checks and review bundles
  -> human review
  -> merge / archive / learn
```

This is different from a coding-agent vendor:

- The worker can be Claude Code, Codex, Grok, Hermes, Cowork, Devin CLI, Factory Droid, OpenHands, or a future agent.
- OM owns context continuity, not model execution.
- OM records decisions and task outcomes into local memory after review.
- OM can sync trusted personal coordination state through OM Cluster later, and exchange scoped task context through OM Mail later.

The product promise becomes:

> "Tell OM what you want done. It remembers the goal, breaks it into reviewable work, sends the right agents, keeps them from clobbering each other, and brings back a clean review queue."

## Core design: Work Control Center

### 1. Manager inbox

A chat-like capture surface for messy intent:

- Voice or text input.
- Paste GitHub issue, stack trace, release checklist, or rough TODOs.
- Attach repo path and target branch.
- Ask the manager to clarify scope before any worker starts.

Output:

- A proposed plan file under `plans/` or `.om/work/`.
- A task graph with dependencies.
- A risk label per task.
- Suggested worker type per task.
- Required checks per task.

### 2. Plain-file task ledger

Use reviewable local files instead of a hidden app database for the source of truth.

Suggested layout:

```text
.om/work/
  inbox.md
  missions/
    2026-06-14-desktop-manager/
      mission.md
      tasks/
        001-doctor-json.md
        002-worktree-adapter.md
      events.jsonl
      artifacts/
      review-bundle.md
```

`mission.md` holds goal, constraints, acceptance criteria, and risk. Each task file holds prompt, owner, dependencies, worktree, status, checks, links to PRs/diffs, and final result.

Why plain files:

- The human can inspect and edit the plan.
- Agents can use normal file tools.
- OM can recall past missions.
- Git can review the manager's plan changes.

### 3. Worker adapter layer

Define a narrow adapter interface:

- `prepare(repo, base_branch, task)`
- `start(task, worktree, context_bundle)`
- `status(run_id)`
- `message(run_id, text)`
- `stop(run_id)`
- `collect(run_id)`

Initial adapters should be local-only:

- Codex CLI.
- Claude Code.
- Grok where installed.
- Generic shell command worker for future agents.

Later adapters:

- Devin CLI or API if available to the user.
- Factory Droid CLI.
- OpenHands local or cloud.
- GitHub issue/PR worker.

The adapter layer should not require OM to understand every vendor's internal state. It should normalize the minimum needed state: queued, running, blocked, needs review, failed, done.

### 4. Worktree isolation

Every coding task runs in its own git worktree:

```text
repo/.om/worktrees/<mission>/<task>/
```

Rules:

- No worker writes directly to the user's active checkout.
- No worker pushes to protected branches.
- Every task starts from the same known base unless explicitly chained.
- The manager records branch name, base commit, and changed files.

### 5. Merge queue

A single local serialization point prevents parallel workers from clobbering each other.

Merge queue states:

1. Waiting for worker.
2. Collecting diff.
3. Running required checks.
4. Reviewer pass.
5. Rebase on queue head.
6. Conflict resolution needed.
7. Ready for human review.
8. Merged to integration branch.
9. Archived.

Important: the merge queue should not merge to `main` by default. It should merge to a human-owned integration branch or produce a review bundle.

### 6. Reviewer and critic passes

For higher-risk tasks, the manager can spawn a reviewer agent that only reads the diff and test output.

Reviewer outputs:

- Summary.
- Risks.
- Missing tests.
- Security/privacy concerns.
- Files that need human attention.
- Suggested follow-up tasks.

The reviewer cannot edit by default. This keeps critique separate from execution.

### 7. Memory integration

OM's memory role is the advantage.

Before delegation:

- Build a scoped context bundle from `om context`, `om recall`, repo docs, and mission files.
- Include project conventions, prior decisions, active plans, and relevant memories.
- Exclude local/private content not allowed by scope.

After completion:

- Summarize accepted outcomes into observations.
- Record durable decisions only after human approval.
- Add task learnings to the relevant project memory.
- Keep failed attempts searchable because they explain future constraints.

### 8. Desktop UI

Add a **Work** tab after the original Memory Control Center tabs.

Work tab sections:

- **Inbox:** capture rough requests.
- **Missions:** current and archived mission list.
- **Plan:** editable mission/task outline.
- **Workers:** live worker runs with logs, status, cost, and stop buttons.
- **Merge Queue:** ordered queue with conflicts, checks, and review status.
- **Review:** diff summary, test output, reviewer notes, and approve/reject controls.
- **Learn:** proposed memory updates from completed work.

Every action should show the equivalent CLI command or file path.

## CLI/API additions needed

Add an `om work` command group before the desktop UI depends on this feature.

Proposed commands:

```bash
om work init --repo .
om work inbox add --text "fix the flaky search tests and update docs"
om work plan MISSION_ID
om work tasks MISSION_ID --json
om work dispatch TASK_ID --worker codex
om work status MISSION_ID --json
om work stop RUN_ID
om work collect RUN_ID
om work queue MISSION_ID --json
om work review TASK_ID
om work merge TASK_ID --to integration/om-work
om work learn MISSION_ID --dry-run
om work archive MISSION_ID
```

The desktop app should consume only JSON or JSONL from these commands.

## Phased plan for the middle-management direction

### Phase A — Research spike and schema

Deliverables:

- Define `.om/work/` file schemas.
- Define worker adapter protocol.
- Define mission/task/run/review event schemas.
- Prototype a read-only desktop mission viewer using fixture files.

Acceptance:

- A mission can be created, edited in a text editor, and rendered in the app without running agents.

### Phase B — Local task ledger and manual queue

Deliverables:

- `om work init`, `inbox`, `plan`, `tasks`, `status`, and `archive`.
- Manual status updates.
- Mission files indexed by OM search.

Acceptance:

- A user can manage agent work as plain files even before automation exists.

### Phase C — Single-worker dispatch

Deliverables:

- Generic shell adapter.
- Codex adapter.
- Worktree creation and cleanup.
- Run logs and result collection.

Acceptance:

- OM can dispatch one task to one worker in one worktree and collect the diff and checks.

### Phase D — Merge queue

Deliverables:

- Queue state machine.
- Required check runner.
- Rebase/conflict detection.
- Integration branch target.
- Human approve/reject gate.

Acceptance:

- Two independent worker branches can be serialized into one integration branch without direct writes to `main`.

### Phase E — Reviewer pass and memory learning

Deliverables:

- Read-only reviewer adapter.
- Review bundle generation.
- `om work learn --dry-run` proposes observations/reflections updates.
- Human approval required for durable memory writes.

Acceptance:

- The manager can explain what changed, what was risky, and what OM should remember.

### Phase F — Desktop Work Control Center

Deliverables:

- Work tab in the desktop app.
- Mission viewer/editor handoff to external editor.
- Worker monitor.
- Merge queue UI.
- Review and learn UI.

Acceptance:

- A desktop user can run a small multi-agent mission without manually juggling terminals or branches.

### Phase G — External orchestrator adapters

Deliverables:

- Devin adapter if the user has Devin access.
- Factory adapter if the user has Droid access.
- OpenHands adapter.
- Optional issue tracker triggers.

Acceptance:

- OM can manage both local and hosted workers through the same mission ledger.

## Adversarial review of the middle-management idea

### Failure mode 1: OM becomes a weak clone of Devin or Factory

Risk: the project chases a full autonomous engineering platform and loses its memory identity.

Mitigation: OM manages context, plans, task state, and review. Worker agents still do the implementation. The source of truth remains local files and git.

### Failure mode 2: multi-agent work is fragile by default

Risk: tasks depend on each other, workers lack shared context, and outputs do not compose.

Mitigation: start with a single manager, explicit task graph, worktrees, and a serialized merge queue. Do not let workers freely delegate to each other in v1.

### Failure mode 3: the manager creates plausible but bad plans

Risk: the user delegates vague goals and the manager decomposes them incorrectly.

Mitigation: require plan approval before dispatch. The planning phase must ask clarifying questions, state assumptions, and list acceptance criteria.

### Failure mode 4: worktree and merge automation loses code

Risk: automated branch cleanup, rebases, or conflict handling discard changes.

Mitigation: never delete worktrees without archived patches. Always record base commit, branch, diff, and event log. Merge only into an integration branch unless the human overrides.

### Failure mode 5: review becomes rubber-stamping

Risk: the UI makes it too easy to accept a batch of agent changes.

Mitigation: show risk labels, changed files, checks, reviewer concerns, and memory-impact proposals. Require explicit human approval for merge and memory writes.

### Failure mode 6: private memory leaks to external hosted agents

Risk: the manager sends too much OM context to Devin, Factory, OpenHands Cloud, or another remote worker.

Mitigation: every context bundle goes through the same scope filter used for cluster/share-out. Remote adapters default to minimal context and show a preview before send.

### Failure mode 7: costs run away

Risk: background workers and reviewer passes consume tokens or hosted-agent credits unexpectedly.

Mitigation: budget per mission, per task, and per worker. Show estimated and actual cost. Pause dispatch when budget is exceeded.

### Failure mode 8: the desktop app becomes a hidden database

Risk: task state lives in app internals and cannot be reviewed or repaired.

Mitigation: `.om/work/` files and `events.jsonl` are authoritative. Any SQLite cache is disposable.

### Failure mode 9: adapters become an integration treadmill

Risk: each vendor changes CLI flags and breaks OM.

Mitigation: ship a generic shell adapter first. Keep vendor adapters small and optional. Test adapters with contract fixtures.

### Failure mode 10: the manager learns the wrong lessons

Risk: failed or low-quality agent output becomes durable memory and poisons future work.

Mitigation: only accepted outcomes become durable memory. Failed attempts can be stored as task history, but reflection into profile/active/reflections requires review.

### Failure mode 11: security model expands too fast

Risk: issue tracker triggers, Slack commands, or mail requests start agents without enough authorization.

Mitigation: local desktop first. External triggers create inbox items, not running tasks, until the user explicitly enables automation with allowlists.

### Failure mode 12: middle management increases, rather than reduces, cognitive load

Risk: users now manage the manager, tasks, workers, queues, and memories.

Mitigation: keep the first UI narrow: inbox, current mission, worker status, review queue. Hide advanced routing until users need it.

## Recommendation

Proceed, but sequence it behind binary installs and desktop memory management.

The right order is:

1. Ship standalone binary installs.
2. Ship Memory Control Center.
3. Add `om work` plain-file mission ledger.
4. Add single-worker worktree dispatch.
5. Add merge queue.
6. Add reviewer and memory-learning gates.
7. Add Work Control Center UI.
8. Add optional hosted-orchestrator adapters.

This keeps the adoption path realistic. Desktop users first get a simple installer and memory manager. Power users later get the middle-management layer that makes parallel agents usable without sacrificing OM's local-first, reviewable trust model.
