# 394 — Cross-Provider Handoff

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

Agents cannot transfer tasks between providers mid-execution. If Claude is struggling with a task, there is no way to hand it off to Codex or Gemini. AWS CAO supports 7+ providers with seamless switching. Bernstein's provider-agnostic claim rings hollow without cross-provider handoff.

## Design

Implement a cross-provider handoff protocol. An agent can explicitly signal that it wants to transfer its task to a different provider (e.g., "this requires code search, hand off to Codex"). The orchestrator receives the handoff request, captures the current task state (context, partial work, files modified), and spawns a new agent with a different adapter. The new agent receives a handoff context document summarizing what was attempted and what remains. Define a standard handoff message format that all adapters understand. Implement handoff triggers: explicit agent request, repeated failures on the same task, and cost optimization (route to cheaper provider for simpler subtasks).

## Files to modify

- `src/bernstein/core/handoff.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `src/bernstein/adapters/base.py`
- `templates/prompts/handoff-context.md` (new)
- `tests/unit/test_handoff.py` (new)

## Completion signal

- Agent A (Claude) can hand off a task to Agent B (Codex) with context preserved
- Handoff context document includes summary of prior work
- Automatic handoff triggers on repeated failures
