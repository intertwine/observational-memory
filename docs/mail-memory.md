# OM Mail (experimental)

OM Mail gives your `om` node its own email inbox and treats memory artifacts as structured email messages. It is **experimental**: a prototype that proves the idea, not a finished feature.

Why email? A dynamic inbox is durable, globally addressable, append-only, and comes with built-in identity (the address) and threading. That means two agents can exchange memory across machines, harnesses (Claude Code, Codex, Grok, Cowork, Hermes), models, orgs, and vendors with no shared infrastructure beyond a mailbox.

## How it differs from OM Cluster sync

[OM Cluster sync](om-cluster-sync.md) replicates *your* memory across *your* trusted nodes: full-state convergence under one shared cluster key. OM Mail is selective, message-grained exchange between *distinct* agents or principals — agent to agent, team to team. They compose: a cluster node can also have a mail address.

## Message kinds

- **memory-note** — a memory item another agent can accept into its observations, flowing through the normal observe → reflect → recall pipeline.
- **context-pack** — a self-contained, scope-filtered, encrypted bundle of profile/active/reflections excerpts mailed to any address.
- **recall-request / recall-response** — agent A emails agent B a question; B answers from its own recall with a signed response on the same thread.

## Security model

- The mail provider is an **untrusted carrier**, same posture as cluster transports. Inbox access is not trust.
- Every OM Mail envelope is **Ed25519-signed**. Inbound messages are verified against the peer key you **pinned locally** with `om mail peers add` — the key embedded in the message is informational only.
- Context packs are **always encrypted** (ChaCha20-Poly1305) with a shared key exchanged **out of band** (a password manager, not email). There is no plaintext-pack mode.
- **Fail closed.** Unknown sender, key mismatch, bad signature, or undecryptable payload means the message is *held* — never ingested, never answered — and shown in `om mail inbox`.
- **Nothing auto-executes.** Notes are ingested only on explicit `om mail accept`, or automatically only from peers you marked `auto_accept`. Recall requests are answered only for peers marked `allow_recall`, and only when you run `om mail sync --respond`.
- **Outbound scope guard.** Every outbound markdown payload passes the share-out scope filter, so `scope=local` content never leaves the host — same rule as cluster.
- Accepted mail content is **untrusted input**: it carries `source=mail:<address>` provenance in the observation block so reflection and audits can see where a fact came from.
- Mail state lives under `<memory_dir>/mail/` (0600, host-local, never synced).

## Quickstart: two agents on one machine

> The `om mail` CLI surface may still evolve. Run `om mail --help` for the current flags.

No AgentMail account needed: the `localdir` provider delivers messages through a shared directory. Give each agent its own memory dir via `XDG_DATA_HOME`.

Agent A's shell:

```bash
export XDG_DATA_HOME=~/om-demo/agent-a
export OM_MAIL_PROVIDER=localdir
export OM_MAIL_LOCALDIR=~/om-mail-demo
om mail init --username agent-a
```

Agent B's shell (a second terminal):

```bash
export XDG_DATA_HOME=~/om-demo/agent-b
export OM_MAIL_PROVIDER=localdir
export OM_MAIL_LOCALDIR=~/om-mail-demo
om mail init --username agent-b
```

`om mail init` prints each agent's address and signing public key. Mint one shared key for encrypted packs and pin each agent as a peer of the other:

```bash
om mail peers new-shared-key       # run once; use the same key on both sides

# In agent A's shell:
om mail peers add agent-b@... --alias agent-b --key <B_PUBKEY> \
  --shared-key <SHARED_KEY> --allow-recall

# In agent B's shell:
om mail peers add agent-a@... --alias agent-a --key <A_PUBKEY> \
  --shared-key <SHARED_KEY> --allow-recall
```

Send a note from A and accept it on B:

```bash
# Agent A:
om mail send-note agent-b@... --text "Bryan prefers short status updates."

# Agent B:
om mail sync
om mail inbox                # shows the pending note and its message id
om mail accept omm_...       # ingests it as an observation
```

Ask B a question from A:

```bash
# Agent A:
om mail ask agent-b@... --query "current work" --wait 30

# Agent B (while A waits):
om mail sync --respond       # answers recall for allow_recall peers only
```

Send a context pack from A to B:

```bash
# Agent A:
om mail send-pack agent-b@...

# Agent B:
om mail sync                 # verified packs open automatically into mail/packs/
ls "${XDG_DATA_HOME}/observational-memory/mail/packs/"
```

`om mail status` shows your account; `om mail search --query "..."` scans your local mail corpus.

## AgentMail setup (live)

For real cross-machine use, get an API key at <https://console.agentmail.to>, then:

```bash
export OM_AGENTMAIL_API_KEY=am_...
export OM_MAIL_PROVIDER=agentmail   # this is the default
om mail init --username my-agent
```

`om mail init` mints a dynamic inbox at `agentmail.to` and prints your address and public key.

## Multi-machine, multi-harness runbook

Validates the full loop across two machines — for example a laptop running Claude Code and a server running Codex. "Multi-model" here means each side's `om` can use a different observer/reflector provider (`OM_LLM_PROVIDER`); memory exchange is model-agnostic.

1. Install `om` on both machines and run `om mail init` on each with `OM_AGENTMAIL_API_KEY` set.
2. Exchange addresses, public keys, and one shared key (`om mail peers new-shared-key`) out of band — a password manager, not email.
3. Pin each machine as a peer of the other with `om mail peers add ... --key ... --shared-key ... --allow-recall`.
4. **Note flow:** on machine A, `om mail send-note <B_ADDR> --text "..."`. On B, `om mail sync`, then `om mail accept <MESSAGE_ID>`. Verify the note landed in `observations.md` with its `source=mail:<address>` provenance, then run `om reflect` and confirm it surfaces in `om recall`.
5. **Recall flow:** on B, `om mail ask <A_ADDR> --query "..." --wait 30`. On A, `om mail sync --respond`. B receives the signed answer.
6. **Pack flow:** on A, `om mail send-pack <B_ADDR>`. On B, `om mail sync`, then inspect the opened files under `<memory_dir>/mail/packs/`.

## Troubleshooting

- **Message held?** `om mail inbox` shows the reason (unknown sender, bad signature, undecryptable payload).
- **Key mismatch?** The peer's pinned key no longer matches. Confirm the new key out of band and re-pin with `om mail peers add`.
- **Pack won't open?** Packs require the out-of-band shared key. Make sure both sides pinned the same `--shared-key`.

## Limits (v1)

- Polling only: `om mail sync` fetches mail; there is no webhook listener or daemon.
- No generic IMAP/SMTP provider yet — the provider seam exists, but only `agentmail` and `localdir` are built.
- Packs open into a review directory (`mail/packs/`); they are not auto-imported into reflections.
- `om mail search` scans the local mail corpus only; no provider-side full-text search.
