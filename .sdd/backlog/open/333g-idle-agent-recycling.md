# 333g — Idle Agent Detection and Recycling

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** small
**Depends on:** #742

## Problem

Agents that finish their task don't always exit cleanly. They sometimes sit idle consuming a slot. Or they're stuck in a loop making no file changes. The orchestrator needs to detect these and recycle the slot.

## Design

### Idle detection (already partially exists in #500)
An agent is "idle" if:
- Process is alive but no file changes for 120s
- Process is alive but no heartbeat update for 90s
- Task is marked "done" or "failed" on server but process still running

### Recycling
When idle detected:
1. Send SHUTDOWN signal (#736)
2. Wait 30s
3. If still alive → SIGKILL
4. Mark agent slot as free
5. Log: "Recycled idle agent backend-abc123 (no activity for 2min)"

### Aggressive mode for self-development
When `--evolve` is active, idle threshold drops to 60s. Self-development needs fast agent turnover.

## Files to modify

- `src/bernstein/core/orchestrator.py` (idle detection in tick)
- `src/bernstein/core/agent_lifecycle.py` (recycling logic)

## Completion signal

- Idle agents detected and killed within 2 minutes
- Freed slots immediately used for new tasks
- No zombie agents consuming resources
