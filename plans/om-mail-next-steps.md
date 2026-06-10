# OM Mail Next Steps — From Prototype to Team-Wide Self-Improving Memory

Status: ACTIVE ROADMAP
Builds on: `plans/email-memory-substrate.md` (shipped prototype), `docs/mail-memory.md`
Related: `plans/team-memory-direction.md` (v1.0 team tier), `docs/om-cluster-sync.md`

## North star

Automated self-improvement over time for every agent on a team. Each agent's
experience (observations → reflections) should raise the floor for every other
agent — across machines, harnesses (Claude Code, Codex, Grok, Cowork, Hermes),
models, and org boundaries — without a human shuttling context by hand, and
without ever widening a trust boundary implicitly. OM Mail v1 proved the
substrate; these phases turn a manual exchange loop into a self-running one.

Every phase stays opt-in and fail-closed. The order is deliberate: ergonomics
first (people must *want* to keep mail flowing), then automation, then breadth,
then the fabric features that make self-improvement compound, then the
governance that lets a real team turn it on by default.

## Phase 1 — Frictionless manual loop (ergonomics)

The v1 pinning ceremony (copy address, copy public key, mint and share a pack
key, run `peers add` on both sides) is the biggest adoption tax.

- `om mail handshake new` / `om mail handshake accept TOKEN`: a single invite
  blob carrying address, signing key, and a wrapped shared-key offer (reuse
  `wrap_key_for_node` X25519 wrapping from cluster invites). One paste per
  side replaces six flag-heavy commands. The token itself is still exchanged
  out of band, preserving the trust model.
- Scheduled sync: `om install --mail-sync` wires `om mail sync --respond`
  into launchd/schtasks on an interval, reusing the existing scheduler
  plumbing (reflect/auto-memory labels). Budget-aware via `om usage` so a
  chatty peer cannot drain a token budget.
- Startup awareness: `om context` gains a bounded one-line mail digest
  ("2 held notes, 1 unanswered ask from <peer>") so agents see pending
  exchange without a new habit. `om doctor` checks mail account health,
  provider reachability, and stale held mail.
- Ask ergonomics: `om recall --peer ADDR --query ...` as sugar over
  `om mail ask`; received recall-responses cached into the local search index
  (with `source=mail:<peer>` provenance) so a question asked once is free
  forever.
- `om mail review`: interactive held-mail triage (show note, accept/reject,
  whole-peer trust upgrades like "always accept from this peer").

## Phase 2 — Event-driven substrate (webhooks, live negotiation)

Polling caps the loop at human cadence. AgentMail ships webhooks and
websockets; use them.

- `om mail listen`: a foreground/daemon mode (stdlib HTTP receiver, same
  posture as `om-relay`) consuming provider webhook events and running the
  exact `mail_sync` routing path per event. Polling remains the fallback;
  idempotency already holds (seen-ids + held quarantine), so webhook and
  poll can coexist safely.
- Live `ask`: with both sides listening, `om mail ask --wait` resolves in
  seconds, making peer recall usable *inside* an agent turn — an agent can
  consult a teammate's memory mid-task, not between sessions.
- Webhook receivers validate provider signatures where offered and treat the
  event only as a "go look" signal — the routed message still passes the full
  envelope verification path. Events never carry trust.

## Phase 3 — Provider breadth

- Generic IMAP/SMTP provider: corporate mail servers and self-hosted stacks
  join the fabric; the envelope already travels as a plain attachment, so
  delivery through Exchange/Gmail is just transport. Tokens/passwords resolve
  through the existing host-local credential store.
- Provider conformance suite: one contract test module run against every
  provider (localdir today, AgentMail stubbed, IMAP against a fixture
  server) so seam drift is caught mechanically.
- Optional provider-side search (`om mail search --remote`) where the
  provider offers full-text search (AgentMail does); local scan stays the
  default and the only path that sees decrypted content.

## Phase 4 — The self-improvement fabric

This is the phase that delivers the north star.

- Reflection digests: a scheduled `om mail digest` mails each peer (or a
  group address) the share-out-filtered *delta* of reflections since the last
  digest. Receivers ingest digests as observations; their own reflection pass
  consolidates team learnings into durable memory. The loop becomes:
  experience → reflect → digest → peers ingest → peers reflect — compounding
  team knowledge with zero human shuttling. Dedup rides the existing
  cross-section dedup plus digest sequence numbers per sender.
- Peer-memory recall index: opened context packs and recall-responses feed
  the local search index as a distinct, provenance-tagged corpus — a
  teammate's knowledge becomes recallable locally without being laundered
  into "my" memory. `om recall` results carry the owner's address.
- Group addresses: one logical team address fans out (provider list or local
  fan-out across pinned peers), so "tell the team" is one send.
- Topic subscriptions: peers advertise interests ("infra", "payments-service",
  cwd/repo scopes); digest senders filter per subscriber so each agent
  receives signal, not everything.
- Quality feedback (research): track which ingested mail facts survive
  reflection vs. get dropped as noise, and report per-peer signal ratios —
  the input for tuning auto_accept and digest filters over time.

## Phase 5 — Team-scale governance

- Team trust roots: pairwise pinning does not scale past a handful of peers.
  A team root key signs peer introductions (certificate-lite); `om mail
  peers add --introduced-by TOKEN` verifies the chain and pins in one step.
  Root rotation and revocation lists ride the same mail substrate.
- `scope=team` activation: widen `SHAREABLE_SCOPES` for the mail share-out
  path only when an explicit team policy file exists — by adding to the
  allowlist, never by editing a filter body (the Gate-4 rule).
- Audit ledger: `om mail log` — an append-only host-local record of every
  send, ingest, answer, and rejection, so "what did my agent tell whom" is
  always answerable. Mail-triggered LLM usage flows into `om usage` budgets.
- Key rotation: re-pin ceremony via handshake tokens; old-key envelopes fail
  closed (already true) with a clear "peer rotated keys" held reason.

## Standing risks and open questions

- Prompt injection via ingested mail: provenance stamps are in place; add a
  reflector instruction that mail-sourced facts are *claims by the sender*,
  not ground truth, and consider an LLM triage pass over held notes that can
  only flag, never accept.
- Pack/message size: providers cap attachments; packs need chunking or
  pack-by-reference (mail carries the manifest, bulk rides a transport) for
  large reflections files.
- Abuse surface of public inboxes: unknown mail is already quarantined;
  rate-limit held-mail growth and expire stale quarantine.
- Digest cadence vs. cost: digests trigger downstream reflection on every
  receiver; budgets and digest deltas (not snapshots) keep this bounded.

## Sequencing and validation

Each phase lands behind its own flag with the same bar as v1: unit tests plus
a localdir end-to-end test proving the new loop, a scope-leak assertion
whenever a new outbound path appears, and a live AgentMail runbook update in
`docs/mail-memory.md`. Phase 1 and 2 are independent of each other; Phase 4
digests depend on Phase 1 scheduling (or Phase 2 listening); Phase 5 can
start any time after Phase 1 handshakes exist.
