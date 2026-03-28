# 346c — OpenCode + Goose Adapters

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

More CLI agents = more users who can use Bernstein. OpenCode and Goose (by Block) are rising open-source alternatives. Each adapter is a gateway to that tool's community.

## Design

Implement two adapters:

### OpenCode adapter
- `src/bernstein/adapters/opencode.py`
- Spawn opencode with prompt via stdin/args

### Goose adapter
- `src/bernstein/adapters/goose.py`
- Spawn goose with task description
- Handle goose's MCP-based extension model

## Files to modify

- `src/bernstein/adapters/opencode.py` (new)
- `src/bernstein/adapters/goose.py` (new)
- `src/bernstein/agents/registry.py`
- `tests/unit/test_adapter_opencode.py` (new)
- `tests/unit/test_adapter_goose.py` (new)

## Completion signal

- Both adapters work end-to-end
- Tests pass
