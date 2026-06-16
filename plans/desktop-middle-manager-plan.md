# OM Desktop as a Middle-Management Layer

Status: PROPOSED EXTENSION
Primary prompt: Sawyer Hood, "I Was Tired of Juggling My Agents, So I Hired a Middle Manager" (https://www.sawyerhood.com/blog/hired-a-middle-manager)
Comparison sources: Devin by Cognition, Factory, Tembo, OpenHands.

## Executive summary

The desktop installer plan should stay the first adoption milestone, but the app can grow into a more valuable layer: a **local middle manager for agent work**.

The opportunity is not to make OM another coding agent. The opportunity is to make OM the persistent, reviewable coordination layer that sits above Claude Code, Codex, OpenCode, Kimi Code CLI, Grok, Hermes, Cowork, Devin-style hosted agents, and future workers. The user talks to one manager. The manager remembers goals, breaks work into tasks, delegates to available agents, tracks work in plain files, serializes merges, runs checks, and keeps the human in control.

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

- The worker can be Claude Code, Codex, OpenCode, Kimi Code CLI, Grok, Hermes, Cowork, Devin CLI, Factory Droid, OpenHands, or a future agent.
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

`om work init` must also add a `.gitignore` entry for `.om/` unless the user opts out. Mission state, task logs, events, and artifacts are local operator state; they should not be committed to the project repo by accident.

Why plain files:

- The human can inspect and edit the plan.
- Agents can use normal file tools.
- OM can recall past missions.
- Git can review the manager's plan changes.

### 3. Worker adapter layer

Define a narrow adapter protocol as CLI contracts, not in-process methods. The desktop app should be able to call `om work` commands over a process boundary and receive JSON output that matches the same schema used by local automation.

Candidate command contracts:

- `om work prepare --task <id> --json`
- `om work start --task <id> --worktree <path> --context-bundle <path> --json`
- `om work status --run <id> --json`
- `om work message --run <id> --text <text> --json`
- `om work stop --run <id> --json`
- `om work collect --run <id> --json`

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
- Define worker adapter command contracts and JSON envelopes.
- Define mission/task/run/review event schemas.
- Prototype a read-only desktop mission viewer using fixture files.

Acceptance:

- A mission can be created, edited in a text editor, and rendered in the app without running agents.
- `om work init` adds `.om/` to `.gitignore` by default so mission state does not get committed accidentally.

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
- OpenHands adapter if the user has a local install or cloud access.
- Optional issue tracker triggers.

Acceptance:

- OM can manage both local and hosted workers through the same mission ledger when the user has installed and authorized those optional adapters.

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
