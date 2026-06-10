# OM Mail — Email Inboxes as a Portable Memory Substrate (experimental)

Status: PROTOTYPE (proving the concept; not part of the v0.8.0 gate sequence)
Inspiration: AgentMail (https://agentmail.to) — API-first, dynamically minted inboxes for agents.
Related: `plans/team-memory-direction.md` (v1.0 team tier), `plans/v0.8.0-plan.md` (Gate 1
portable bundles, Gate 4 scope filter), `docs/om-cluster-sync.md` (trust posture).

## 1. The idea

A dynamic email inbox is a durable, portable, globally addressable, append-only
message store with built-in identity (the address), threading, and search.
Those are exactly the properties of a memory substrate. OM Mail gives each om
node its own inbox and treats memory artifacts as structured email messages:

- **memory-note** — append a memory item to another agent's (or your own)
  memory. Accepted notes become observations and flow through the normal
  observe → reflect → recall pipeline.
- **context-pack** — a self-contained, scope-filtered, encrypted memory bundle
  (profile/active/reflections excerpts + manifest) mailed to any address.
- **recall-request / recall-response** — query negotiation: agent A emails
  agent B's memory address a question; B answers from its own recall with a
  signed response on the same thread.

Because the substrate is email, this works across machines, harnesses
(Claude Code, Codex, Grok, Cowork, Hermes), models, orgs, and vendors with no
shared infrastructure beyond a mailbox — the enterprise "fleets of agents in
different harnesses sharing context" story.

This is deliberately a different shape from OM Cluster sync. Cluster
replicates *one person's* memory across *their* trusted nodes (full-state
convergence, shared cluster key). OM Mail is *selective, message-grained
exchange between distinct principals* (agent↔agent, team↔team). The two
compose: a cluster node can also have a mail address.

## 2. Trust model (decided up front)

- The mail provider is an **untrusted carrier**, same posture as cluster
  transports. Relay access ≠ trust; inbox access ≠ trust.
- Every OM Mail envelope is **Ed25519-signed**. Inbound messages are verified
  against a **pinned peer key** in a local address book (`peers.toml`). The key
  embedded in the envelope is informational; the pinned key decides.
- Context packs (and any payload to a peer with a shared key) are
  **ChaCha20Poly1305-encrypted** with a symmetric key exchanged **out of band**
  (never over email). Packs REQUIRE a shared key — there is no plaintext-pack
  mode.
- **Fail closed everywhere**: unknown sender, key mismatch, bad signature,
  undecryptable payload, malformed envelope → the message is *held* (never
  ingested, never answered) and surfaced in `om mail inbox`.
- **Nothing auto-executes.** Inbound notes are ingested only on explicit
  `om mail accept`, or automatically only from peers explicitly marked
  `auto_accept`. Recall requests are answered only for peers marked
  `allow_recall`, and only when the operator runs `om mail sync --respond`
  (no daemon in v1).
- **Outbound scope guard**: every outbound markdown payload passes through the
  Gate-4 share-out resolver (`filter_reflection_document_for_shareout`).
  `scope=local` content never leaves the host, same rule as cluster.
- Mail state is host-local under `<memory_dir>/mail/` (0600), never synced —
  same rule as `usage.sqlite` and `.provider-jobs/`.
- Inbound mail content is **untrusted input to prompts**: accepted notes carry
  `source=mail:<address>` provenance in the observation block so reflection and
  audits can see where a fact came from.

## 3. Architecture

```text
om mail send-note/send-pack/ask  ->  MailProvider.send_message  ->  peer inbox
peer inbox  ->  om mail sync  ->  verify/decrypt  ->  accept -> observations.md
                              \->  recall-request + --respond -> local recall -> recall-response
```

New package `src/observational_memory/mail/`:

- `provider.py` — `MailProvider` protocol (create_inbox / send_message /
  list_messages / get_message), message dataclasses, factory. The provider
  seam keeps "mailbox" a role, not a vendor (`team-memory-direction.md` §4).
- `providers/agentmail.py` — AgentMail REST client (stdlib urllib, Bearer
  `OM_AGENTMAIL_API_KEY`, base `https://api.agentmail.to/v0`). Dynamic inbox
  minting via `POST /inboxes`; send with base64 attachments; list with
  `after`/`page_token` cursors; attachment download via `download_url`.
- `providers/localdir.py` — directory-backed provider (one JSON file per
  message under a shared root). Used by tests and by local multi-process
  demos; also works machine-to-machine over any shared folder.
- `envelope.py` — the wire format: versioned JSON envelope shipped as the
  `om-mail.json` attachment (bodies get munged by mail systems; attachments
  are byte-faithful). Canonical-JSON Ed25519 signature; optional
  ChaCha20Poly1305 payload encryption bound to the envelope identity via AAD.
  Reuses `sync/crypto.py` primitives unchanged.
- `account.py` — host-local state: `account.toml` (inbox + signing keypair),
  `peers.toml` (pinned keys, shared keys, allow_recall / auto_accept flags),
  `state.json` (sync cursor + seen ids), `held/` (quarantined inbound).
- `pack.py` — context-pack build/open: collects profile/active/reflections,
  runs the share-out filter, manifests with SHA256, encrypts.
- `service.py` — the operations the CLI wraps: sync (fetch → verify → route),
  ask/respond negotiation, note ingestion via the existing observation append
  path.

CLI: `om mail` group — `init`, `status`, `peers add/list/remove`, `send-note`,
`send-pack`, `ask`, `sync [--respond]`, `inbox`, `accept`, `reject`,
`open-pack`, `search`.

## 4. Wire format (envelope v1)

Email shape: human-readable subject `[om-mail] <kind>` + short text body for
humans; the machine artifact is the `om-mail.json` attachment:

```json
{
  "om_mail": 1,
  "id": "omm_<128-bit hex>",
  "kind": "memory-note | context-pack | recall-request | recall-response",
  "request_id": "omm_... of the request (responses only)",
  "sent_at": "2026-06-10T12:00:00Z",
  "sender": {"address": "...", "alias": "...", "signing_public_key_b64": "..."},
  "payload_encrypted": false,
  "payload": { ... kind-specific, or {"encrypted": EncryptedPayload} },
  "signature_b64": "ed25519 over canonical JSON minus signature_b64"
}
```

Payloads:

- memory-note: `{"subject", "markdown"}` (markdown is share-out-filtered at send)
- context-pack: `{"created_at", "host_alias", "manifest": {file: sha256_...},
  "files": {"profile.md": "...", "active.md": "...", "reflections.md": "..."}}`
- recall-request: `{"query", "limit"}`
- recall-response: `{"recall_status": "ok|empty|unavailable", "results":
  [{"rank", "heading", "content", "source_path"}]}`

Encryption AAD = canonical JSON of `{"id", "kind", "om_mail", "sender_address"}`
so a ciphertext cannot be replayed under a different envelope identity.

## 5. What this proves / non-goals (v1)

Proves: dynamic inbox minting as agent identity; durable cross-machine
memory exchange with no shared infra; agent↔agent recall negotiation;
scope-governed, encrypted context sharing; provider-portability via the seam.

Non-goals now: webhooks/daemon listening (poll via `om mail sync`), generic
IMAP/SMTP provider (seam documented, not built), pack auto-import into
reflections (packs open into a review dir), provider-side full-text search
(local corpus scan only), any hosted multi-tenant service (v1.0 territory).

## 6. Validation

- Full test suite over the localdir provider and an in-memory fake: two-agent
  ask/respond negotiation end-to-end, scope-leak prevention, signature/
  encryption fail-closed paths, held-message quarantine, idempotent sync.
- AgentMail client unit-tested against stubbed HTTP; live multi-machine
  runbook in `docs/mail-memory.md` (requires `OM_AGENTMAIL_API_KEY`).
