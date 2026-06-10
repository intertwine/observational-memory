# Release Notes — v0.8.0

## Theme

v0.7.0 made reflection scale. v0.8.0 makes memory **trustworthy**: durable (backup and restore), provable (provenance, scope governance, conflict surfacing), and conversational (`om talk`) — plus an experimental preview of **OM Mail**, which gives agents email inboxes so they can exchange memory across machines, harnesses, models, and vendors.

Everything is additive. Defaults are unchanged, and OM Mail is CLI-only and off until you set it up.

## Durability — `om backup` / `om restore` (Gate 1)

Your memory can now defend itself against a bad write or an accidental delete:

```bash
om backup --reason pre-experiment   # on-demand snapshot
om backup --list                    # what snapshots exist
om restore --latest                 # byte-faithful restore (safety snapshot first)
```

- **Automatic pre-reflect snapshot.** Every `om reflect` saves the last-good state before it writes, so a bad reflection can always be rolled back.
- Restore is **byte-faithful** and takes a safety snapshot of the current state first.
- Rotating retention bounds disk use. Snapshots are host-local and never synced.

## Talk to your memories — `om talk` (#80, Gate 2)

A spoken-style conversation with your own memory. Each turn runs recall in the background and grounds the reply in what it finds:

```bash
om talk --query "what was I working on last week?"
```

v0.8.0 also makes the recall layer honest under failure: "recall timed out / backend unavailable" is now distinguished from "no relevant memory found" in every recall consumer, with a `OM_TALK_RECALL_TIMEOUT` knob. A new Moss backend joins `bm25`/`qmd` for recall. Text-only for now; voice providers are planned on the same loop.

## Provenance and scope governance (Gates 3–4)

Memory you can audit, with sharing rules that fail closed:

- **Section provenance stamps.** Each reflection section carries `last_reflected` and `derived_from_obs_window` — when it was last derived and from which observation window. Retrieval objects carry typed `owner`/`scope`/`source` copies; inline Markdown stays authoritative.
- **Pluggable scope governance.** All share-out paths — cluster snapshots, cloud search upload, and OM Mail — route through one resolver with a single allowlist (`SHAREABLE_SCOPES`). Unknown scopes are denied by default; `scope=local` content never leaves your machine by any path.

## Conflict surfacing — `om reflect --check-conflicts` (Gate 5)

Reflection can silently change a high-stakes fact. Now you can see it:

```bash
om reflect --check-conflicts            # reflect as normal, plus a conflict report
om reflect --dry-run --check-conflicts  # audit only — nothing is written
```

The check itself is a read-only advisory that always exits 0: it diffs the prior `reflections.md` against the new one and flags explicit-id divergence, slot divergence, and guardrail downgrades, writing a throwaway review artifact. Note that without `--dry-run` the reflection still writes as usual — the flag adds the report, it does not make `om reflect` read-only. Precision-first: it flags only what it is confident about. `--json` for machines.

## Growth measurement in `om doctor` (Gate 6)

`om doctor` and `om context --quality-report` now measure memory growth: per-document and per-section sizes, the largest section's share, and how cold each section is — from real timestamps only, never a guess. Read-only and fail-soft; it exists so any future compaction decision is grounded in data instead of speculation.

## Experimental preview — OM Mail (#88)

Agents get their own email inboxes and exchange memory as structured, signed, encrypted messages — notes, context packs, and recall requests ("ask my peer's memory a question"). Email makes the exchange durable, globally addressable, and vendor-neutral: it works across machines, harnesses (Claude Code, Codex, Grok, Cowork, Hermes), models, and orgs with no shared infrastructure beyond a mailbox.

Trust model in five bullets:

- The mail provider is an **untrusted carrier**; inbox access is not trust.
- Every envelope is **Ed25519-signed** and verified against a peer key you **pinned locally**.
- Context packs are **always encrypted** with a key exchanged out of band.
- **Fail closed:** unknown sender, bad signature, or undecryptable payload is held for review, never ingested.
- **Nothing auto-executes**, and `scope=local` never leaves the host — including through recall answers.

Try it with two agents on one machine (no account needed, `localdir` provider), or live with [AgentMail](https://agentmail.to) inboxes: see [docs/mail-memory.md](mail-memory.md).

v0.8.0 also ships the public **plugin seams**: out-of-tree mail providers and CLI add-ons plug in through entry points (`observational_memory.mail_providers`, `observational_memory.cli_plugins`), with contributor terms in [CONTRIBUTING.md](../CONTRIBUTING.md). Pairwise mail is MIT core and stays that way; team-fabric features (digests, trust roots, group addresses) are planned as separately licensed add-ons on these seams.

## Smaller fixes

- `om reflect --async` now rejects a cross-provider reflector model (e.g. a leftover `grok-*` or Claude model) **before** submitting the OpenAI Batch job, instead of letting the batch fail hours later (#77).
- The test suite is hermetic by default: ambient `OM_*` and provider env vars no longer leak into test runs.

## Compatibility

- All changes are additive; no defaults changed. New surfaces (`om backup`, `om talk`, `om mail`, `--check-conflicts`) activate only when you call them.
- One new background behavior: `om reflect` takes an automatic pre-reflect snapshot. Retention keeps disk use bounded.
- OM Mail is experimental: the CLI surface may evolve, and it is CLI-only (no hooks, no daemon, polling only).

## Validation

- Full suite green on Python 3.11/3.12/3.13 in CI; 1,059 tests at release.
- Every feature PR passed an adversarial review pass (Codex) before merge; findings (e.g. an AgentMail pagination-order bug, a timestamp-tie sync bug, a localdir path-escape, a fine-tuned-model guard gap) were fixed with regression tests.
- OM Mail was validated **live across two machines** (laptop ↔ Linux VM over real AgentMail inboxes): note flow with held→accept→provenance, recall request/response, encrypted context pack, and a wire-level check that only ciphertext crosses the provider.

## Out of scope / next

- **v0.9.0 is reserved for OM Mail GA** — handshake tokens, live listening, digests: see [plans/om-mail-next-steps.md](../plans/om-mail-next-steps.md).
- The recall-quality benchmark (#81) runs in `observational-memory-bench` and will inform a future decision on default backend / fusion.
- The full memory-unit store and destructive compaction remain deferred behind Gate-6 data (#71).
