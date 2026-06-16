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

Start with **PyApp**. Use **Nuitka standalone** as the fallback if PyApp cannot pass the integration test matrix with the current dependencies.

Required properties:

- No Python install required on the target machine.
- Reproducible builds from locked dependencies.
- `om --version`, `om doctor`, `om context`, `om recall`, `om talk`, `om cluster`, and `om mail` work from the packaged binary.
- The binary can locate and write the same data and config paths documented for the Python install.
- The binary can be code signed and notarized where the platform expects it.
- The build plan defines which targets are Tier 1 and which are best effort before promising all artifacts.

### Desktop app

Use **Tauri v2** for the first implementation.

Reasons:

- Good fit for a small local UI that mostly shells out to an existing binary.
- Native installers are available for macOS, Windows, and Linux.
- The Rust side can own process management, file opening, update checks, and OS integration.
- The web UI can stay simple and testable.

Avoid Electron unless Tauri blocks a required feature. The app should not feel larger than the memory tool it manages.

Tauri adds a second toolchain. Phase 2 must add `src-tauri/Cargo.toml`, Rust/Cargo in CI, and a Node package workflow for the web bundle before desktop builds become required checks.

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

Phase 0 must define the binary resolution algorithm:

- Whether each desktop installer bundles `om` or downloads it on first run.
- Where a downloaded binary lives on each OS.
- Whether an existing `om` on PATH is reused, ignored, or shown as an alternate install.
- The GitHub Release URL pattern and checksum/provenance verification flow.
- Repair behavior when the app binary and PATH binary disagree.

Phase 1 must define signing-secret handling:

- Apple Developer ID certificate, notarization credentials, and keychain setup.
- Windows Authenticode certificate or signing service requirements.
- Tauri updater signing key handling if in-app updates are enabled.
- Cosign or checksum signing for Linux artifacts.

## Desktop UI requirements

### 1. Welcome and install

The first run should show:

- What OM does in plain English.
- Where memory and config will live.
- A button to install or repair the `om` binary.
- A provider setup path that branches between subscription login (`om login`) and API-key provider setup (`om install --provider ...`).
- A final **Run Doctor** step.

The UI should show the equivalent CLI command for every action in a collapsible details area. This keeps the product teachable without making the command line mandatory.

### 2. Doctor dashboard

Expose `om doctor` as the app home screen.

Show:

- Overall status: healthy, needs attention, or broken.
- Provider status.
- Hook install status for Claude Code, Codex, OpenCode, Kimi Code CLI, Grok, Hermes, and Cowork where available.
- Search index status and a **Reindex** button.
- Backup status and latest snapshot.
- Memory growth and quality warnings.
- Cluster status if configured.
- Mail status if configured.

Implementation requirement: use `om doctor --json` and stabilize any missing fields the dashboard needs. The desktop app should not scrape human text.

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

Implementation requirement: `om talk --json` already emits an end-of-session transcript. Add a turn-level protocol such as `om talk --jsonl` or `om talk serve --stdio` so the UI can avoid brittle terminal automation. Phase 0 should define the message envelope, recall citation shape, and recall status values before implementation.

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

Implementation requirement: most cluster surfaces already have JSON output (`status`, `requests`, `sync`, and peer listing). Phase 0 should inventory the exact desktop flows and add JSON only for missing gaps such as invite creation metadata or disable/repair flows.

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

Implementation requirement: mail status, peer listing, sync, inbox review, and ask already have JSON output. Phase 0 should scope the remaining gaps precisely, especially accept, reject, send-note, and send-pack.

## Engineering plan

### Phase 0 — Audit and contracts

Deliverables:

- Inventory every CLI command the desktop app needs.
- Add stable JSON contracts for missing command outputs.
- Document data paths and command behavior for the app.
- Add golden tests for the JSON schemas.
- Define the `om talk` turn-level JSONL or stdio schema.
- Define the desktop binary resolution and repair algorithm.

Acceptance:

- The UI team can build against documented command contracts.
- Human CLI output can change without breaking the app.

### Phase 1 — Binary CLI artifacts

Deliverables:

- Build scripts for all target triples.
- Smoke tests that run each packaged binary in a clean temp home.
- Artifact checksums and provenance.
- Tier-1 target matrix and runner strategy for macOS, Windows, and Linux.
- Signing dry runs in CI, with real signing on release runners.
- Secret-management runbook for signing and updater keys.

Acceptance:

- A user can download `om`, put it on PATH, and run `om doctor` without Python or `uv`.

### Phase 2 — Desktop shell

Deliverables:

- Tauri app skeleton.
- Rust/Cargo and Node build toolchain in CI.
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

## Future Work Control Center Plan

The broader OM Desktop middle-management direction now lives in `plans/desktop-middle-manager-plan.md`. Keep this file focused on binary packaging, desktop install, and the Memory Control Center adoption path.
