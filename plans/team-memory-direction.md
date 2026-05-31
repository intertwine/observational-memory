# Team & Enterprise Memory Direction (toward 1.0)

Status: STRATEGY NOTE (no 1.0 work committed yet)
Related: PR #80 (`om talk` + Moss backend), issue #71 (scaling), `plans/talk-to-memories.md`

This note records the agreed direction for cloud-hosted **team / enterprise
memory** so the decision and its constraints are written down against the 0.7/0.8
scaling work. It deliberately commits *no* 1.0 implementation — only the cheap,
reversible "keep the door open" moves listed in §6.

## 1. Roadmap framing

| Release | Memory scope | Recall | Conversation / voice |
| --- | --- | --- | --- |
| **0.7.x** | Personal, host-local | `bm25` (default), `qmd` / `qmd-hybrid` local; `moss` opt-in cloud accelerator | `om talk` — **experimental, text-only** |
| **0.8.0+** | Personal, host-local; keep scaling per #71 | Same, plus the keep-the-door-open hooks (§6) | **Pluggable voice providers** (mic/STT/TTS) on the existing `VoiceTransport` seam |
| **1.0** | Personal **+** shared team/org | Local backends serve personal recall; a **shared semantic recall layer** serves cross-team | "Chat / search across team memories" as a thin feature on top |

The headline features (voice, chat-with-team-memories) are intentionally a
*cherry on top* of a broadly useful recall + governance substrate. The substrate
drives the design; the demos do not.

## 2. Architectural thesis: two independent axes

om's search backends vary on two axes that should not be conflated:

- **Capability:** lexical (`bm25`) vs semantic (`qmd-hybrid`, `moss`).
- **Trust model:** on-host (`bm25`, `qmd`) vs off-host/cloud (`moss`).

`bm25` ↔ `qmd-hybrid` is a *capability* upgrade within one trust model. `moss`
is a *trust-model* jump. The team/enterprise tier is fundamentally a second
trust model living beside the personal one — not an extension of it. Making
`moss` "just another backend with explicit trade-offs" (PR #80) is the precedent
that makes the team tier a backend + governance problem rather than a rewrite.

## 3. The trust-model fork (decide consciously; do not inherit cluster's posture)

OM Cluster's spine is: transports are **untrusted**, content is **E2E-encrypted**,
relay access ≠ trust, the user holds the keys. A semantic **cloud** index is the
inverse: the server must see **plaintext** to embed and rank it (Moss embeds
server-side; even client-side embeddings ship vectors the server indexes and can
partially invert). **You cannot have both E2E-encryption and server-side
semantic search over the same bytes.**

So "enterprise cousin of cluster" is a good story but a misleading blueprint.
The team tier must pick, explicitly:

- **(a) Trusted server** — encryption at rest + tenant isolation + access
  control; the server sees plaintext. This is what enterprise RAG actually is,
  and is acceptable *if* data-residency / processor / self-host / SLA questions
  are answered.
- **(b) Encrypted + client-side embeddings** — server does ANN over vectors it
  can't read back to text. Preserves more of cluster's posture; limited rerank,
  vector-leakage risk, much harder.

Most likely outcome: **(a) for the team tier, (b) (today's cluster) for personal
sync — two trust models side by side**, not one extended.

## 4. "Shared semantic recall" is a role, not a vendor

The durable abstraction is the *role* — a shared semantic recall provider —
behind the existing `SearchBackend` (or a richer "memory provider") seam. Moss is
one implementation. An enterprise deployment will likely want a **self-hostable**
option (pgvector / qdrant / a relay-backed vector store) as the default, with a
hosted service as convenience. Keep the seam at the interface, never bind "team
layer" to a single SaaS.

## 5. Governance is the 80%; retrieval is the 20%

Personal om has one trust boundary (the host) and one scope flag (`local` vs
`cluster`). Team memory adds: a scope **lattice** (`local → team → org`),
per-team/project visibility, redaction, retention / right-to-delete, audit, and
contributor attribution. The recall backend is the easy part; the
scope/authorization layer is the hard part. The seed already exists in
`reflection_metadata` scopes and the `filter_*_for_cluster` materialization
filters — that is where the lattice should grow.

## 6. Keep-the-door-open hooks for 0.8 (cheap now, expensive to retrofit)

Build *none* of the 1.0 tier yet. Only preserve optionality:

1. **Keep recall multi-backend-aware.** Don't let `om talk` / recall assume a
   single backend. The real team architecture is "personal local index **+**
   shared index(es), merged at query time," not one giant cloud index.
2. **Generalize the scope filter.** Evolve `filter_reflection_entries_for_cluster`
   into a scope-aware pipeline (`local / team / org`) rather than a
   cluster-specific function. This is the governance seed.
3. **Carry provenance on `Document` / `SearchResult`** (owner, scope, source /
   team) so a future multi-source merge can dedup and rank scope-aware.
4. **Prototype the *merge* against local backends first** — e.g. local `bm25`
   plus a second "shared" `bm25`/`qmd` index — proving scope-aware
   merge/dedup/ranking with **zero cloud dependency** before committing to any
   vendor or trust model.

## 7. Out of scope until 1.0 (explicit)

- Any hosted multi-tenant team store, auth, or billing.
- Server-side access control / RBAC, audit, retention enforcement.
- Binding the team tier to Moss (or any single vendor).
- E2E-vs-trusted-server crypto decision **implementation** (the *decision* is
  framed in §3; the build waits).

## 8. Open questions to resolve before 1.0 work starts

- Trusted-server (a) vs encrypted-client-embeddings (b) — and is self-host
  mandatory for the enterprise default?
- Does the scope lattice live in reflection metadata, or a separate ACL layer?
- Is "team memory" one shared index per team, or federated per-project indexes
  merged at query time?
- How does `scope=local` / `scope=personal` interact with team materialization
  (reuse the cluster filters, or a distinct governance pass)?
