# Open Knowledge Format Adoption Plan

## Summary

Adopt the Open Knowledge Format (OKF) as an optional interchange format for Observational Memory, not as the primary on-disk memory store yet.

The right near-term target is:

- `om export --format okf` writes a reviewable OKF bundle.
- `om import --format okf` reads a reviewed OKF bundle into staging or normal local memory after explicit confirmation.
- Native memory files, OM Cluster records, search indexes, and usage data stay as they are.

This is a limited-go recommendation. OKF is promising because it matches the way this project already treats memory: plain files, Markdown, local review, and agent-readable context. But OKF v0.1 is new and intentionally small. It does not yet answer enough questions about private memory, provenance, conflict handling, deletion, or sync semantics to become the core storage contract for this repo.

## What OKF is

Google introduced OKF on June 12, 2026 as a vendor-neutral format for AI-ready knowledge bundles. The format is simple: a directory of Markdown files, one concept per file, with YAML frontmatter for a small set of structured fields.

OKF v0.1 requires a `type` field. It also defines common fields such as `title`, `description`, `resource`, `tags`, and `timestamp`. The file path is the concept identity. Normal Markdown links create relationships between concepts. Optional `index.md` files support progressive browsing, and optional `log.md` files carry chronological history.

The design goal is portability, not a new service. A bundle should be readable in an editor, reviewable in git, indexable by normal search tools, and usable by agents without an SDK.

## Why this fits Observational Memory

Observational Memory already has many OKF-shaped traits:

- Long-term memory is Markdown-centered.
- Startup context is a budgeted projection, not the whole store.
- Recall and search already separate retrieval from startup payload size.
- Hosted memory exports are review bundles, not silent writes to managed memory systems.
- OM Cluster is opt-in and has its own trust and sync boundaries.
- The repo is local-first and reviewable by design.

OKF could give this project a standard way to exchange memory with other agents, catalogs, and knowledge tools without inventing another bespoke bundle format.

## What should become OKF concepts

Use OKF concepts for stable knowledge units, not raw transcripts or every observation line.

Suggested concept types:

| OKF type | Source in Observational Memory | Notes |
| --- | --- | --- |
| `om.profile` | durable profile sections | Preferences, working style, identity, and stable facts. |
| `om.project` | active project sections | One file per project or major workstream. |
| `om.decision` | durable decisions | Useful when a decision has a date, rationale, and links. |
| `om.runbook` | docs or operational memory | Setup steps, recovery steps, release steps. |
| `om.observation-summary` | reflected summaries | Summaries of observation windows, not raw sensitive logs. |
| `om.reference` | external docs or repo docs | Links and summaries that explain memory context. |
| `om.cluster-note` | reviewed cluster materialization | Only if the user opts in to cluster export. |

Do not export these by default:

- Raw transcripts.
- Usage tracking data from `usage.sqlite`.
- Provider keys, tokens, node private keys, request secrets, `data_keys`, private hostnames, IPs, or tunnels.
- Unreviewed hosted-memory write targets.
- Cluster records unless the user explicitly asks for cluster content.

## Proposed bundle layout

Start with a conservative bundle shape:

```text
okf-bundle/
  index.md
  profile/
    index.md
    working-style.md
    preferences.md
  projects/
    index.md
    observational-memory.md
  decisions/
    index.md
    okf-interchange.md
  runbooks/
    index.md
  observations/
    index.md
    2026-06.md
  references/
    index.md
    source-docs.md
  log.md
```

Example frontmatter:

```yaml
---
type: om.project
title: Observational Memory
description: Current durable project memory for Observational Memory.
tags: [observational-memory, project]
timestamp: 2026-06-13T00:00:00Z
om_source: reflections.md#active-projects
om_visibility: private-review
om_export_version: 1
---
```

The `om_*` fields are extension fields. Consumers that only understand OKF can ignore them. OM can use them to preserve provenance and privacy review state.

## CLI proposal

### Export

```bash
om export --format okf --out ./memory-okf
```

Useful flags:

```bash
om export --format okf --out ./memory-okf --include-profile
om export --format okf --out ./memory-okf --include-projects
om export --format okf --out ./memory-okf --include-observation-summaries
om export --format okf --out ./memory-okf --include-cluster-reviewed
om export --format okf --out ./memory-okf --redact
om export --format okf --out ./memory-okf --dry-run
```

Default behavior should be safe:

- Export durable reflected memory only.
- Redact known dangerous fields.
- Write a manifest of included files.
- Print a warning that the bundle may still contain private memory and should be reviewed before sharing.
- Never include usage data.
- Never include raw transcripts unless a future explicit flag is added.

### Validate

```bash
om okf validate ./memory-okf
```

Validation should check:

- Every concept has YAML frontmatter.
- Every concept has `type`.
- Reserved names are used as intended.
- Internal Markdown links resolve.
- OM extension fields are valid when present.
- Known secret-like values are flagged.

### Import

```bash
om import --format okf ./memory-okf --stage
om import --format okf ./memory-okf --apply
```

Import should be staged first. A staged import writes a review report and proposed patches, not durable memory changes.

Apply should require an explicit flag and should preserve provenance:

```bash
om import --format okf ./memory-okf --apply --confirm-reviewed
```

## Implementation phases

### Phase 0: Decide and document

- Land this plan.
- Open an issue or follow-up plan for OKF export/import.
- Track OKF upstream changes because v0.1 is young.

Exit criteria:

- Maintainer agrees that OKF is an optional interchange format, not core storage.

### Phase 1: Export-only proof of concept

Implement a small exporter with no import path.

Scope:

- Add `om export --format okf --out <dir>`.
- Convert existing reflected sections into OKF concept files.
- Write `index.md` files and a top-level `log.md`.
- Preserve source pointers in `om_source` fields.
- Add tests for path generation, frontmatter, redaction, and link validation.

Pushback:

- Do not change native memory files.
- Do not add sync behavior.
- Do not add hosted-memory writes.

Exit criteria:

- A user can export a local OKF bundle and inspect it in git.
- The exporter passes lint and tests.

### Phase 2: Validator and review workflow

Scope:

- Add `om okf validate`.
- Add secret and sensitive-field checks.
- Add an export manifest with counts, source files, and warnings.
- Support `--dry-run` for export.

Exit criteria:

- The tool can tell a user whether a bundle is structurally OKF-compatible and whether OM-specific safety rules pass.

### Phase 3: Staged import

Scope:

- Add `om import --format okf <dir> --stage`.
- Parse OKF concepts into proposed memory updates.
- Produce a review bundle with diffs.
- Do not write durable memory during staging.

Exit criteria:

- A user can see exactly what an OKF import would change before applying it.

### Phase 4: Apply reviewed imports

Scope:

- Add `--apply --confirm-reviewed`.
- Merge only supported concept types.
- Preserve `om_source`, timestamps, and import provenance.
- Fail closed on conflicts, missing required fields, or unresolved links when they matter.

Exit criteria:

- Reviewed OKF can become local memory without silent overwrite or data loss.

### Phase 5: Reassess native storage

Only revisit native storage if OKF adoption becomes broad and stable.

Questions to answer first:

- Is OKF still compatible with OM's sectioned reflection model?
- Can OKF represent deletions, tombstones, conflicts, and provenance well enough?
- Can it preserve privacy and cluster trust boundaries?
- Are there multiple real consumers, not only Google examples?

## Reasons not to adopt OKF as the core standard today

1. **OKF v0.1 is immature.** The spec is intentionally small and may change.
2. **Privacy is not a first-class spec feature.** OM needs strong defaults around private memory and secrets.
3. **No sync semantics.** OKF does not define merge rules, trust, cluster membership, tombstones, or conflict resolution.
4. **Weak provenance model.** OM needs to know where a memory came from and whether it was user-reviewed.
5. **Concept granularity may not match reflection sections.** One-file-per-concept is useful, but OM's durable memory currently uses routed sections and budgeted startup projections.
6. **YAML frontmatter can become schema drift.** Too many extension fields would reduce interoperability.
7. **Markdown links are helpful but not enough.** Search, recall handles, and startup context need stable IDs and budget hints.

These are reasons for a limited adoption, not a rejection.

## Mitigations

- Treat OKF as import/export first.
- Keep OM-native memory as the source of truth.
- Add OM extension fields only where needed.
- Validate links and required fields.
- Stage imports by default.
- Redact dangerous values and warn about private content.
- Never include usage data or raw transcripts by default.
- Keep cluster export opt-in and clearly labeled.
- Track upstream OKF spec versions in exported manifests.

## Open questions

- Should OM concept IDs use file paths only, or should OM add stable `om_id` fields?
- Should active project subsections become one concept per project or one concept per section?
- Should `om recall` read OKF bundles directly, or should OKF import into native memory first?
- Should OKF export include source line references from search metadata when available?
- What secret scanner should be used before a bundle can be shared?

## Recommendation

Proceed with export-only OKF support after this plan is reviewed. Do not replace native storage, sync records, or startup-context routing with OKF in the first implementation.

The adoption path should be:

1. Optional export.
2. Validation and review safety.
3. Staged import.
4. Reviewed apply.
5. Later reassessment for deeper storage alignment.

This gives Observational Memory the upside of a shared, agent-friendly knowledge format while protecting the project's local-first, private-by-default design.

## Adversarial review

An adversarial review reached the same limited-go position, with stronger warnings around export leaks and import poisoning.

Main critiques:

1. **A limited export path is still an exfiltration path.** Markdown bundles are easy to commit, attach, or paste. Redaction and review must be part of the feature, not optional cleanup.
2. **Import preview is not enough by itself.** Users can click through warnings. Imported OKF should start as quarantined, low-trust knowledge and should not become behavioral memory unless promoted.
3. **Plain OKF cannot preserve full OM semantics.** Full-fidelity round trips need OM extension metadata. Plain OKF should be treated as best-effort exchange only.
4. **The standard may not last.** OKF v0.1 could become maintenance debt if adoption does not broaden.
5. **Google's examples are data-catalog oriented.** Observational Memory is personal and private by default, so the project should not copy data-catalog assumptions wholesale.

Response:

- Keep the feature isolated behind explicit commands.
- Keep native memory as the source of truth.
- Make export redacted and scoped by default.
- Make import staged and low-trust by default.
- Do not sync OKF through OM Cluster.
- Do not write OKF directly to hosted memories.
- Remove or freeze the adapter if upstream OKF changes in a way that conflicts with OM safety rules.
