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
transcripts into shared observations and reflections across all sessions.

## What's Available

Memory is automatically loaded at session start via the SessionStart hook.
The injected context includes:

- **Profile** — stable facts about the user (role, preferences, communication
  style) extracted from long-term reflections.
- **Active context** — current projects, recent themes, and in-flight work
  derived from recent observations.

## Searching Memory

To search past observations and reflections, run the `om` CLI:

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
3. **Startup priming** — at session start, compact derived files (profile.md
   and active.md) are injected as context.

## When to Use This Skill

- User asks about something discussed in a previous session
- User wants to recall a decision, preference, or project status
- User asks "what do you know about me" or similar identity questions
- Context from prior sessions would improve the current response
