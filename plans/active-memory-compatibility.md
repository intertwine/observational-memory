# Active Memory Compatibility Plan

Date: 2026-05-07

## Sources Checked

- OpenAI Help: [Memory FAQ](https://help.openai.com/en/articles/8590148-memory-in-chatgpt)
- OpenAI Help: [ChatGPT Pulse](https://help.openai.com/en/articles/12293630-chatgpt-pulse)
- OpenAI Help: [Projects in ChatGPT](https://help.openai.com/en/articles/10169521)
- Anthropic Docs: [Using agent memory](https://platform.claude.com/docs/en/managed-agents/memory)
- Anthropic Blog: [Built-in memory for Claude Managed Agents](https://claude.com/blog/claude-managed-agents-memory)
- Anthropic Blog: [New in Claude Managed Agents: dreaming, outcomes, and multiagent orchestration](https://claude.com/blog/new-in-claude-managed-agents)

## Platform Shape

### OpenAI ChatGPT Memory

ChatGPT Memory currently has two main user-facing memory controls: saved memories and reference chat history. Saved memories are durable personalization notes, while reference chat history lets ChatGPT search past chats when relevant. Project memory can further scope what prior context is visible inside a project. Memory sources can now expose some of the context that influenced a response, including past chats, saved memories, custom instructions, and, on eligible plans/regions, files and Gmail.

ChatGPT Pulse is the more active layer: it does asynchronous daily research based on past chats, memories, feedback, and optionally connected apps. It requires memory to be enabled.

Compatibility implication: OM should not pretend it can write saved ChatGPT memories directly. The compatible shape is a concise, user-reviewed seed that can be pasted into ChatGPT, attached to a project, or uploaded as a reference file. It should emphasize stable facts and preferences, not exact templates or raw transcripts.

### Anthropic Claude Managed Agents Memory And Dreaming

Claude Managed Agents memory stores are workspace-scoped text-document collections. Stores attach at session creation, mount inside the agent environment, and can be managed through API or Console. Individual memories are capped at 100KB, and Anthropic recommends many small focused files instead of a few large ones. Stores support read-only and read-write access; Anthropic warns that writeable memory exposed to untrusted input can become a persistent prompt-injection path.

Anthropic's May 6, 2026 dreaming preview sits on top of memory stores. Dreaming is a scheduled process that reviews agent sessions and memory stores, extracts patterns, restructures memory, and can either update memory automatically or wait for human review. Anthropic positions dreaming as especially useful for long-running work and multi-agent orchestration.

Compatibility implication: OM's markdown memory is already close to the Claude memory-store shape, but it needs an export bundle that splits long-term reflections into small focused files, includes a manifest, and recommends read-only imports unless the user intentionally wants agents to write back.

## Compatibility Plan

1. Keep OM local-first and reviewable. `observations.md`, `reflections.md`, `profile.md`, and `active.md` remain the source of truth.
2. Add `om export` as the compatibility bridge:
   - `--target chatgpt` writes one concise `chatgpt-memory-seed.md` plus instructions.
   - `--target claude-managed-agents` writes a `memories/` directory with small focused files and a manifest.
   - `--target generic` writes a simple markdown bundle for other memory systems.
3. Exclude raw observations by default because they are transient. Allow `--include-observations` for users who deliberately want recent session notes included.
4. Do not implement provider-side memory writes yet:
   - OpenAI does not expose a stable saved-memory write API for ChatGPT Memory.
   - Anthropic Managed Agents memory APIs are usable, but direct writes would require auth, workspace/store selection, and review semantics that should be explicit.
5. Future work:
   - Add an Anthropic memory-store import helper once the Managed Agents API stabilizes and a safe review flow is designed.
   - Add an OpenAI direct import path only if OpenAI exposes an official saved-memory or project-memory write API.
   - Add optional redaction/review tooling before export for users who want stricter personal-data controls.
