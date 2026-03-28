# 708 — Interactive Session Streaming (Crystal-killer)

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Crystal (by Stravu) lets you watch, inspect, and manage parallel Claude Code sessions in real-time. Conductor shows a central dashboard. Users want to SEE what agents are doing, not just see task status. Bernstein's TUI shows task completion but not agent output streams. This is the "wow" factor that makes demos compelling and users feel in control.

## Design

### Live agent output streaming
Each spawned agent's stdout/stderr is captured and streamed to:
1. The TUI (in the right panel per-agent)
2. SSE endpoint `/agents/{session_id}/stream`
3. Log file `.sdd/runtime/logs/{session_id}.log`

### Agent inspection
- See what each agent is currently doing (file being edited, command being run)
- See token count / cost accumulating in real-time per agent
- Ability to send a message to a running agent (stdin injection)

### Web dashboard agent view
Extend the existing web dashboard with a per-agent panel showing:
- Live output stream (auto-scroll)
- Current file being edited
- Tokens used / cost so far
- Kill button

## Files to modify

- `src/bernstein/core/spawner.py` (capture stdout)
- `src/bernstein/core/server.py` (SSE endpoint)
- `src/bernstein/tui/app.py` (agent output panel)
- `tests/unit/test_session_streaming.py` (new)

## Completion signal

- `bernstein live` shows real-time agent output in TUI
- Web dashboard shows per-agent live output
- Agent output persisted to log files
