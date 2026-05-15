---
name: observational-memory
description: >
  Cross-session observational memory system. Use when the user asks to "recall
  something from a past session", "search memory", "what do you know about me",
  "remember when we discussed", or needs context from previous conversations.
  Also triggers when the user mentions "observational memory", "OM", or asks
  about cross-session context.
---

# Observational Memory

You have access to a persistent memory system that compresses conversation
transcripts into shared observations and reflections across sessions.

## What's Available

Memory is loaded at session start through the SessionStart hook. The injected
context is budgeted, so it may include recall handles for deeper sections.
The startup pack includes:

- **Profile** — stable facts about the user (role, preferences, communication
  style) extracted from long-term reflections.
- **Active context** — current projects, recent themes, and in-flight work
  derived from recent observations.

## Recalling Memory

Use `om recall` for agent-friendly retrieval:

```bash
om recall --query "query terms" --limit 10 --json
```

Expand a startup handle when one appears in the context:

```bash
om recall --handle startup:active:active-projects
```

Use `om search` when you want direct search output and source metadata:

```bash
om search "query terms" --limit 10 --json
```

This searches across three document sources:
- **Observations** — compressed transcripts organized by date
- **Reflections** — consolidated long-term memory organized by topic
- **Auto-memory** — Claude Code per-project memory files

## How It Works

1. **Observer** — after each session, an LLM compresses the conversation
   transcript into prioritized observations (🔴 high / 🟡 medium / 🟢 low).
2. **Reflector** — daily, observations are consolidated into structured
   reflections covering identity, projects, preferences, and themes.
3. **Startup priming** — at session start, `om context` injects a budgeted
   pack built from `profile.md` and `active.md`.
4. **Recall** — during a session, `om recall` can expand startup handles or
   search deeper memory.

## When to Use This Skill

- User asks about something discussed in a previous session
- User wants to recall a decision, preference, or project status
- User asks "what do you know about me" or similar identity questions
- Context from prior sessions would improve the current response
