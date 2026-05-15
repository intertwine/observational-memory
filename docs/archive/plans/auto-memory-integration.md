# Plan: Auto-Memory Integration into Observational Memory

## Context

Claude Code's built-in auto-memory stores per-project discrete facts (user preferences, feedback, project state, references) in `~/.claude/projects/<project>/memory/*.md`. Codex has an empty `~/.codex/memories/` directory (planned but unimplemented). Currently, om only ingests session **transcripts** from Claude and Codex — it has no visibility into these already-distilled memory stores.

This creates two problems:
1. **Redundant storage** — om's reflector may re-derive facts that auto-memory has already captured
2. **Missing cross-project signal** — auto-memory facts from project A are invisible when working in project B

The fix: add `claude-memory` as a third input source. Auto-memory files are already distilled, so they **bypass the observer LLM** and flow directly into the search index and reflector.

```
~/.claude/projects/*/memory/*.md    (read-only scan)
    ↓ content-hash change detection
    ↓ parse into Document objects (DocumentSource.AUTO_MEMORY)
    ↓
Search Index ←── alongside observations + reflections
    ↓
Reflector ←── supplementary context (cross-project facts)
    ↓
reflections.md (enriched)
```

## Implementation

### Step 1: Extend DocumentSource enum

**File:** `src/observational_memory/search/__init__.py`

Add `AUTO_MEMORY = "auto_memory"` to the `DocumentSource` enum (line 11).

### Step 2: New scanner module

**New file:** `src/observational_memory/transcripts/auto_memory.py`

Core functions:

- `find_memory_directories(claude_projects_dir: Path) -> list[Path]` — glob `*/memory/` under `~/.claude/projects/`
- `scan_memory_files(memory_dir: Path) -> list[MemoryFile]` — enumerate `.md` files, compute sha256 content hashes, extract project slug from parent dir name
- `parse_memory_file(mf: MemoryFile) -> Document` — strip optional YAML frontmatter, create one `Document` per file with `doc_id="amem:<project-slug>/<stem>"`, `source=AUTO_MEMORY`, frontmatter metadata (name, type, description) in `metadata` dict
- `detect_changes(cursor: dict, files: list[MemoryFile]) -> tuple[list[MemoryFile], list[str]]` — compare content hashes against cursor's `"claude-memory"` key, return `(changed_files, deleted_doc_ids)`

Project slug extraction: sanitized dir names like `-Users-bryanyoung-experiments-hive-orchestrator` → take last two segments → `hive-orchestrator`.

Cursor structure:
```json
{
  "claude-memory": {
    "files": {
      "/full/path/to/MEMORY.md": {
        "hash": "sha256...",
        "last_seen": "2026-03-24T10:30:00Z"
      }
    },
    "last_scan": "2026-03-24T10:30:00Z"
  }
}
```

### Step 3: Search parser integration

**File:** `src/observational_memory/search/parser.py`

Add `parse_auto_memory(claude_projects_dir: Path) -> list[Document]` — calls `scan_memory_files` + `parse_memory_file` for each project directory.

### Step 4: Reindex integration

**File:** `src/observational_memory/search/__init__.py`

In `reindex()` (line 58), add after reflections:
```python
from .parser import parse_auto_memory
documents.extend(parse_auto_memory(config.claude_projects_dir))
```

### Step 5: QMD backend source detection

**File:** `src/observational_memory/search/qmd.py`

Add `amem:` prefix handling in source detection logic so QMD search results correctly identify auto-memory documents.

### Step 6: observe_auto_memory function

**File:** `src/observational_memory/observe.py`

New function — **no LLM call**, unlike `observe_all_claude`/`observe_all_codex`:

```python
def observe_auto_memory(config: Config | None = None, dry_run: bool = False) -> list[str]:
    """Scan Claude Code auto-memory files and update the search index.

    Unlike transcript observers, this does NOT call the LLM —
    auto-memory files are already distilled facts. It detects
    changed files via content hashing and triggers a reindex.
    """
```

1. Scan all memory directories
2. Detect changes via content hashing against cursor
3. Update cursor with new hashes
4. Trigger reindex (which now includes auto-memory documents)
5. Return list of changed file paths

### Step 7: CLI wiring

**File:** `src/observational_memory/cli.py`

- Extend `--source` choice to include `"claude-memory"` (alongside `claude`, `codex`, `all`)
- Add `claude-memory` branch in `observe()` command
- Include in `--source all` flow (after claude + codex)
- Update `status` command to report auto-memory stats (project count, file count)
- Update `backfill` command's source choices

### Step 8: Reflector supplementary context

**File:** `src/observational_memory/reflect.py`

Add helper `_gather_auto_memory_context(config: Config) -> str` that reads all auto-memory files and formats them as:

```markdown
## Auto-Memory (cross-project facts)

### Project: hive-orchestrator
- Dense retrieval scoring must not override FTS ranking invariants...

### Project: claude-code-scheduler
- Architecture: shell + Python 3, 9 files...
```

Inject into `_reflect_single()` and `_reflect_chunked()` user prompts as a third section after observations.

**File:** `src/observational_memory/prompts/reflector.md`

Add brief instruction: "Auto-memory notes are distilled facts from Claude Code's per-project memory system. Integrate relevant facts into appropriate reflection sections. Do not duplicate — merge with existing knowledge."

### Step 9: Cron integration

**File:** `src/observational_memory/cli.py` (`_install_cron()`)

Add hourly auto-memory scan (no LLM calls, just hash comparison + reindex):
```
0 * * * * ... om observe --source claude-memory 2>/dev/null
```

### Step 10: Future-proofing for Codex memories

No code changes now. The scanner module structure (find directories → scan files → parse → detect changes) is reusable. When `~/.codex/memories/` gets populated:
- Add `codex-memory` source
- Add `DocumentSource.CODEX_MEMORY`
- Reuse the same scanning/hashing/cursor pattern

## Critical files

| File | Change |
|------|--------|
| `src/observational_memory/search/__init__.py` | `DocumentSource.AUTO_MEMORY`, `reindex()` |
| `src/observational_memory/transcripts/auto_memory.py` | **New** — scanner, parser, change detection |
| `src/observational_memory/search/parser.py` | `parse_auto_memory()` |
| `src/observational_memory/search/qmd.py` | `amem:` prefix handling |
| `src/observational_memory/observe.py` | `observe_auto_memory()` |
| `src/observational_memory/cli.py` | `--source claude-memory`, status, cron |
| `src/observational_memory/reflect.py` | `_gather_auto_memory_context()` |
| `src/observational_memory/prompts/reflector.md` | Auto-memory instruction |

## Safety invariants

- **Read-only**: om NEVER writes to `~/.claude/projects/*/memory/` — auto-memory directories are strictly read-only input
- **No observer LLM**: auto-memory bypasses the observer entirely (already distilled)
- **Idempotent**: content hashing means repeated scans with no changes produce no work
- **Graceful degradation**: missing/empty memory directories are silently skipped

## Verification

1. `uv run pytest tests/test_auto_memory.py` — unit tests for scanner, parser, change detection, slug extraction, frontmatter stripping
2. `uv run python -m observational_memory.cli observe --source claude-memory --dry-run` — verify scanning finds all 6 project memory directories
3. `uv run python -m observational_memory.cli observe --source claude-memory` — verify cursor updates in `.cursor.json`
4. `uv run python -m observational_memory.cli search "dense retrieval scoring"` — verify auto-memory documents appear in search results
5. `uv run python -m observational_memory.cli reflect --dry-run` — verify reflector receives auto-memory context
6. `uv run python -m observational_memory.cli status` — verify auto-memory stats in output
7. Modify a memory file, re-run observe — verify change detection works
8. `make check` (or equivalent lint/test suite) — ensure no regressions
