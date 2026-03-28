# 705 — Cursor CLI Adapter

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

Cursor is the dominant AI IDE. Its CLI mode enables programmatic control. Supporting Cursor means Bernstein can orchestrate the tool most developers already use. This is a "good first issue" for community contributors.

## Design

Implement `src/bernstein/adapters/cursor.py`:
- Spawn cursor CLI with appropriate flags
- Map Bernstein model configs to Cursor's model settings
- Handle Cursor's file editing patterns
- Register in adapter registry

## Files to modify

- `src/bernstein/adapters/cursor.py` (new)
- `src/bernstein/agents/registry.py`
- `tests/unit/test_adapter_cursor.py` (new)

## Completion signal

- `bernstein -g "task" --cli cursor` works
- Tests pass
