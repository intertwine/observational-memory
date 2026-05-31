# Plan: `om talk` — Voice Conversation With Your Memories (Moss-backed recall)

Status: REVISED v2 (design reviewed adversarially + SDK verified)
Positioning: experimental text-only in 0.7.x; pluggable voice in 0.8.0+; shared
team/enterprise recall is a 1.0 direction — see `plans/team-memory-direction.md`.
Owner: feature branch `claude/om-voice-memories-recall-TLhIa`
Issue tie-in: "talk with your memories" using https://www.moss.dev

## 1. What the user asked for

> A "talk with your memories" feature in Om, using moss.dev. It should feel
> like a voice conversation with Om while, in the background, the system runs
> recall over memories.

## 2. Key reality check on moss.dev

Moss (`www.moss.dev`, `pip install moss`) is **NOT a voice/STT/TTS service**.
It is a **real-time semantic-search runtime**: sub-10 ms local-first vector
search built for grounding conversational AI. Its own marketing positions it
as "the retrieval engine that makes voice agents faster and smarter."

So the feature splits cleanly into two layers:

- **Recall layer (Moss):** a new Om search backend that runs fast semantic
  recall over `observations.md` / `reflections.md` / auto-memory. This is the
  literal "running recall over memories in the background" piece, and the
  moss.dev tie-in.
- **Voice layer (pluggable):** a conversation loop that listens, grounds each
  reply in recalled memory, and speaks back. Voice I/O is a pluggable
  transport so the *logic* is testable headless and real audio is opt-in.

This split matters: it keeps the moss.dev integration honest (it is a search
engine, used as one) and keeps the heart of the feature — memory-grounded
conversation — fully implemented and tested even without audio hardware.

## 3. Architecture

```
                 ┌─────────────────────── om talk loop ───────────────────────┐
 user speaks ──▶ │ VoiceTransport.listen()  ─▶ utterance                       │
                 │      │                                                       │
                 │      ├─▶ RecallEngine.recall(utterance)  (background thread) │
                 │      │        └─▶ SearchBackend.search()  ← Moss / bm25 / qmd│
                 │      │              (fast, sub-10ms with Moss)               │
                 │      ▼                                                       │
                 │ Conversation.reply(utterance, recalled, history)            │
                 │      └─▶ llm.compress(system, user)  ← existing brain        │
                 │      ▼                                                       │
 Om speaks  ◀──  │ VoiceTransport.speak(reply_text)                            │
                 └─────────────────────────────────────────────────────────────┘
```

Reuse, do not reinvent:
- `llm.compress(system_prompt, user_content, config, operation="talk")` — the
  existing provider-routed, budget-gated, usage-tracked brain.
- `search.get_backend()` / `SearchBackend` protocol for recall.
- `startup_memory.build_startup_payload()` for the opening "who am I talking to"
  grounding pack.

## 4. Components

### 4.1 Moss search backend — `search/moss.py`
Implements the existing `SearchBackend` protocol (`index`, `search`, `is_ready`).
Wraps the async `moss` SDK (`MossClient(project_id, project_key)`,
`create_index`, `load_index`, `query`). Bridges async→sync on a private event
loop so it satisfies the sync protocol like bm25/qmd.

- `index(documents)`: upsert one Moss document per `Document` (`id=doc_id`,
  `text=content`, `metadata={source, heading, date, ...}`), then ensure the
  index is loaded.
- `search(query, limit)`: `query(index, text, top_k=limit, alpha=...)` →
  `list[SearchResult]`, mapping metadata back into `Document`.
- `is_ready()`: SDK importable **and** credentials present **and** index
  loadable. Fail closed (return `False` / `[]`) on any of: missing `moss`
  package, missing creds, network/SDK error. Never raises into the caller.

Registered in `search/__init__.py::get_backend` as `"moss"`.

Config (mirrors the `OM_QMD_*` pattern in `config.py`):
- `OM_MOSS_PROJECT_ID`, `OM_MOSS_PROJECT_KEY` (creds; key never logged/printed)
- `OM_MOSS_INDEX_NAME` (default `observational-memory`)
- `OM_MOSS_ALPHA` (hybrid blend, default unset → SDK default)
- `OM_MOSS_TOP_K` cap, `OM_MOSS_EMBED_MODEL` (minilm|mediumlm) optional.

### 4.2 Recall engine — `talk/recall.py`
`RecallEngine(config, backend=None)`:
- `recall(query, limit) -> RecallResult` — runs `backend.search`, dedupes,
  trims each snippet to a budget, returns structured snippets + source labels.
- `recall_async(query, limit) -> Future[RecallResult]` — submits recall to a
  `ThreadPoolExecutor` so the listen loop is never blocked ("in the
  background"). The next turn can pre-warm while Om is still speaking.
- Degrades cleanly: if `backend.is_ready()` is False, returns an empty result
  (conversation still works, just ungrounded) — never crashes the loop.

### 4.3 Conversation brain — `talk/conversation.py`
`Conversation(config, recall_engine, ...)`:
- Holds rolling history (bounded turns).
- `reply(utterance) -> ConversationTurn`: kicks off background recall, builds a
  spoken-style system prompt ("You are Om, the user's memory companion. Answer
  from the recalled memory snippets. Keep replies short — they are spoken
  aloud. If memory has nothing relevant, say so plainly."), assembles user
  content (recalled snippets + recent history + utterance), calls
  `llm.compress(..., operation="talk")`, records the turn.
- The opening turn injects a trimmed `build_startup_payload()` so Om knows who
  it is talking to.

### 4.4 Voice transport — `talk/transport.py`
`VoiceTransport` protocol: `listen() -> str | None` (None = end), `speak(text)`,
`close()`.
- `TextTransport` (default; always available): stdin in, stdout out. Makes the
  feature runnable and fully testable headless. This is the fallback when no
  TTY or audio deps.
- `AudioTransport` (opt-in): mic capture + playback via optional
  `sounddevice`/`soundfile`, STT via `openai` `audio.transcriptions`, TTS via
  `openai` `audio.speech`. Lazy imports; if deps/creds missing it raises a
  single clear "install `observational-memory[voice]` / configure OPENAI_API_KEY"
  message, and `om talk` auto-falls-back to text with a warning.

### 4.5 CLI — `om talk`
```
om talk [--text/--voice] [--backend moss|bm25|qmd|none]
        [--limit N] [--for AGENT] [--max-turns N] [--query SEED]
```
- Default transport: `--voice` if a TTY + audio deps + creds, else `--text`.
- Default backend: configured `OM_SEARCH_BACKEND`; `--backend moss` overrides.
- Prints a running transcript; `--json` emits the structured transcript at end.
- Mirrors the existing `recall` command's Click style and `ctx.obj["config"]`.

### 4.6 Packaging & docs
- `pyproject.toml`: new optional extra `voice = ["moss>=...", "sounddevice", "soundfile"]`.
- `docs/talk-to-memories.md`: setup, config, examples, headless behavior.
- README one-liner pointer; `docs/configuration.md` gets the `OM_MOSS_*` /
  `OM_TALK_*` rows; `docs/search-and-recall.md` notes Moss as a backend.
- `om doctor` row for Moss backend readiness (creds present? SDK importable?).

## 5. Privacy / safety (respect CLAUDE.md OM Cluster + secrets rules)
- Moss creds are host-local config; **never** printed, logged, or synced.
- Moss is treated like qmd: a local/edge index of *already-local* memory. We do
  not push memory to Moss cloud unless the user sets cloud creds — document
  this clearly. Default guidance: local/on-device index.
- `scope=local` reflection entries must not leak; recall reads the same files
  the existing backends read, which already honor local scope at write time.

## 6. Testing strategy (headless-first)
- `tests/test_search_moss.py`: Moss backend against a fake in-memory SDK
  (monkeypatched import) — index/search/is_ready, fail-closed on missing
  creds/SDK, metadata round-trip.
- `tests/test_talk_recall.py`: RecallEngine dedup/trim/async, empty-backend
  degradation.
- `tests/test_talk_conversation.py`: brain grounds reply in recalled snippets
  (monkeypatch `llm.compress`), history bounding, opening startup injection.
- `tests/test_cli_talk.py`: `CliRunner` drives `om talk --text` with scripted
  stdin and a fake backend + fake `compress`; asserts transcript + JSON.
- All tests run with no network, no audio, no Moss package installed.

## 7. Scope boundaries (explicitly OUT for this PR)
- No real-time streaming/barge-in audio; turn-based voice only.
- No new LLM provider; reuse `llm.compress`.
- No writing the conversation back as observations (note as follow-up).
- No release/version bump (CLAUDE.md release boundary).

## 8. Adversarial review gates
1. Design review (this doc) → revise.
2. Implementation review (post-code) → fix.
Both via independent reviewer agents with a "find the holes" brief.

---

## 9. Design-review resolutions (v2) — verified facts + decisions

An independent adversarial review (staff-engineer brief) plus a real SDK spike
(`pip download moss==1.2.0` + introspection) changed the design materially.

### 9.1 Verified Moss SDK surface (was a HIGH-risk guess)
- `MossClient(project_id, project_key)` — **all methods are `async`** (the Rust
  core calls are wrapped in `asyncio.to_thread`).
- `DocumentInfo(id, text, metadata=None, embedding=None)`
- `create_index(name, docs, model_id="moss-minilm") -> MutationResult`
- `add_docs(name, docs, options=MutationOptions(upsert=...)) -> MutationResult`
- `load_index(name, auto_refresh=False, polling_interval_in_seconds=600)` —
  **downloads** the index from the cloud into memory for ~1–10 ms local queries.
- `query(name, query, options=QueryOptions(embedding, top_k, alpha, filter)) -> SearchResult`
  - `SearchResult(docs, query, index_name, time_taken_ms, model_id)`
  - `docs: list[QueryResultDocumentInfo(id, text, metadata, score, index_name)]`
  - Runs in-memory iff the index is loaded; otherwise hits the cloud query API.

### 9.2 BLOCKER resolved — Moss is cloud-backed, so privacy is explicit
Verified: `create_index` / `add_docs` upload document **text** through the Rust
`ManageClient` to `service.usemoss.dev`. Indexing memory into Moss therefore
**uploads private memory to a third party**. Resolution (honors both the user's
"use moss.dev" request and CLAUDE.md host-local privacy):

- **`om talk` is local-by-default.** It uses the *configured* search backend,
  which is `bm25` by default — fully on-host, zero upload. The voice + recall
  experience works with no cloud at all.
- **Moss is an explicit, opt-in accelerator.** It only activates when the user
  deliberately sets `OM_SEARCH_BACKEND=moss` *and* provides creds.
- **Local-scope never leaves the host.** Before indexing, the Moss adapter
  drops any `Document` whose content carries `scope=local` markers (same rule
  as `filter_reflection_entries_for_cluster`). Extends CLAUDE.md's "scope=local
  must not become shared cluster memory" to cloud upload.
- **Loud consent.** `index()` prints a one-line "uploading memory text to Moss
  cloud (service.usemoss.dev)" notice; docs + `om doctor` state it plainly.
  No secrets (`OM_MOSS_PROJECT_KEY`) are ever logged.

### 9.3 Async→sync bridge — concrete (was "private event loop", hand-wavy)
One persistent event loop on a single daemon thread, owned by the backend for
its lifetime. The `MossClient` is created once on that loop. Calls cross via
`asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=...)`. No
per-call loop teardown, no "client bound to a dead loop", no reentrancy from
overlapping loops. `close()` stops the loop/thread.

### 9.4 Scope cuts (keep the PR reviewable)
- **`AudioTransport` cut** to a documented follow-up. It had zero test coverage
  by the plan's own admission and dragged in PortAudio/libsndfile. Ship
  `TextTransport` only; the memory-grounded conversation is fully deliverable
  and testable without audio. The `voice` extra ships **only** `moss`.
- **Pre-warm-while-speaking cut.** v1 runs exactly one in-flight background
  recall per turn (a `ThreadPoolExecutor` future the brain waits on with a
  timeout). Still genuinely concurrent / "in the background", no reentrancy.

### 9.5 Indexing trigger + graceful degradation (were unspecified)
- `om talk` calls `Conversation.prepare()` once: for a Moss backend it
  `load_index`es; `om talk --reindex` (re)builds first. If the backend is not
  ready, recall returns empty and the conversation runs ungrounded — never
  crashes. `om talk --reindex` / `om search --reindex` (re)build the index.
- The talk loop **catches** `BudgetExceededError` and provider `RuntimeError`
  from `llm.compress` and degrades to a short spoken apology, then continues.
  Tested explicitly.

### 9.6 Secondary fixes folded in
- `get_backend` gains a `"moss"` branch **and** its error string is updated.
- Every `OM_MOSS_*` param defaults to the SDK default (wrong guess → SDK
  default, not a crash). The adapter catches `AttributeError`/`TypeError` from
  any API mismatch and fails closed.

---

## 10. Implementation-review resolutions (post-code)

A second adversarial review (staff-engineer brief) ran against the committed
diff. Findings addressed:

- **[BLOCKER] `_AsyncLoop` resurrected a closed loop.** `close()` now sets a
  final `_closed` flag; `_ensure()` raises instead of rebuilding the loop/thread.
  A timed-out `run()` cancels the future and closes the coroutine. Tested
  (`test_close_is_final_no_resurrection`).
- **[BLOCKER] missing `om doctor` Moss row.** Added: SDK-installed + creds rows,
  stating recall uploads to the cloud, never printing the key. Verified no key
  leak.
- **[SHOULD-FIX] delete-then-create could leave no index.** Replaced with
  upsert-in-place + create-on-first-use (`add_docs(upsert=True)`, the verified
  SDK primitive); corrected the docstring; documented the staleness trade-off.
  Tested (`test_index_creates_then_upserts`).
- **[SHOULD-FIX] scope filter was section-granular.** Now strips `scope=local`
  *lines* (exactly matching `filter_reflection_entries_for_cluster`), so mixed
  sections still upload their shared lines. Tested
  (`test_mixed_scope_section_uploads_shared_lines_only`).
- **[SHOULD-FIX] metadata int→str round-trip.** Known int fields
  (`source_line`, `source_start_line`) are restored to int on read so Moss hits
  match bm25/qmd shape. Tested.
- **[SHOULD-FIX] `_is_budget_error` fragile name match.** Now `isinstance`
  against the real `BudgetExceededError`, with the name-string only as fallback.
  Tested against the real class (`test_real_budget_exception_is_detected`).
- **[NIT] cold-load pause.** CLI prints "Preparing recall backend…" before the
  (possibly slow) Moss `load_index` in `prepare()`.
