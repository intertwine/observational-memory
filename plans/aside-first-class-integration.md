# First-Class Aside Support Plan

**Status:** Implemented on branch `feat/aside-first-class` (pending review/merge + release).

## Goal

Promote [Aside](https://aside.com) (agentic browser) from an ad-hoc `--source cowork`
impersonation to a first-class OM peer alongside Claude Code, Codex, Grok, Cowork,
and Hermes.

Users should be able to:

- Have Aside sessions contribute observations through `om observe --source aside`
  (auto-discovery + single-transcript).
- Read warm startup context from Aside via `om context --for aside`.
- See Aside reported in `om status`.

## Why Aside is different

Aside has **no native file-hook system**. It runs its own daemon and orchestrates
work through skills and routines, so there is no `~/.aside/.../hooks.json` to install
into (unlike Claude/Codex/Grok) and no host plugin tree (unlike Cowork). Aside is
therefore an **observe-first peer**, the same shape as the manual Hermes path:

- **Write-back** is a real, native OM ingestion path (`om observe --source aside`).
- **Warm start** is wired on the Aside side through an `om` skill that reads OM's
  plain-Markdown stores and calls `om context --for aside`; OM ships no installer.

## Transcript format

Sessions live at:

```text
~/.aside/u/<user-index>/agents/<agent>/sessions/<date>_<session-id>/messages.jsonl
```

One JSON object per line, keyed by `role` (not `type`), with **no per-message UUID**
and millisecond epoch timestamps:

- `user` — `content` is a string.
- `assistant` — `content` is a list of typed blocks: `text`, `thinking`, `toolCall`
  (`{type,id,name,arguments}`); also carries `provider`/`model`/`responseId`.
- `toolResult` — tool output (skipped; the `toolCall` summary carries the signal).
- `system-message` — skill/site notices (skipped).

Because there is no UUID, resumption is **count-based** (the Codex/Grok strategy),
not cursor-by-uuid (the Claude/Cowork strategy).

## Key decisions

- New `transcripts/aside.py` parser: native role/block parsing, `thinking` dropped,
  `toolCall` summarized against Aside's real lowercase tool registry (`bash`, `repl`,
  `read_file`, `edit_file`, `websearch`, `webfetch`, ...), ms→ISO-8601 timestamp
  normalization, and `messages.jsonl` discovery via
  `u/*/agents/*/sessions/*/messages.jsonl`.
- Count-based cursor in `observe_aside_transcript` (cursor = number of user/assistant
  messages already observed; rotation-safe reset when the stored index exceeds the
  current message count).
- No backfill subcommand wiring — matches the Grok precedent (Grok is also count-based
  and is intentionally absent from `om backfill` choices).
- `ASIDE_HOME` env override (mirrors `GROK_HOME`/`CODEX_HOME`).
- No `om install --aside` — there is no hook surface to install into.

## Delivered surface

- `om observe --source aside` (auto-discover) and `om observe --transcript <messages.jsonl> --source aside`
- `om observe --source aside --dry-run`
- `om context --for aside` / `om recall --for aside` (free-form host routing already supported)
- `om status` "Aside" section (sessions dir + discovered session count)
- `Config.aside_home` + `Config.aside_sessions_root`
- Aside section in `docs/integrations.md`; Agent Support row in `README.md`
- Aside-side `om` skill shipped in the wheel at
  `src/observational_memory/aside/skills/om/SKILL.md`
- Tests: `tests/test_transcripts.py::TestAsideParser`, `tests/test_observe.py::TestAsideObserver`

## Files changed

- `src/observational_memory/transcripts/aside.py` (new)
- `src/observational_memory/transcripts/__init__.py` (docstring/source label)
- `src/observational_memory/observe.py` (`observe_aside_transcript`, `observe_all_aside`)
- `src/observational_memory/config.py` (`aside_home`, `aside_sessions_root`)
- `src/observational_memory/cli.py` (source enum, transcript dispatch, source detection,
  scan loop, `om status` section, `--for` help text)
- `src/observational_memory/aside/skills/om/SKILL.md` (new; shipped skill)
- `docs/integrations.md`, `README.md`
- `tests/test_transcripts.py`, `tests/test_observe.py`

## Validation

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest tests/test_transcripts.py::TestAsideParser tests/test_observe.py::TestAsideObserver
.venv/bin/python -m pytest tests/test_transcripts.py tests/test_observe.py tests/test_cli_observe.py
om observe --source aside --dry-run        # "Scanning Aside sessions..." then no-op on empty ASIDE_HOME
om status                                  # shows the Aside section
```

## Release steps (maintainer)

- Decide the version bump (new host = minor, e.g. 0.8.0 -> 0.9.0) and update
  `pyproject.toml` + `src/observational_memory/__init__.py`.
- Add a release note (e.g. `docs/RELEASE-0.9.0.md`) and regenerate the Homebrew formula.

## Follow-up ideas

- Optional Aside-side routine that calls `om observe --source aside` on session end,
  giving Aside a checkpoint cadence comparable to the hook-based hosts.
- Watch for an Aside plugin/hook surface; if one ships, add `om install --aside`.
