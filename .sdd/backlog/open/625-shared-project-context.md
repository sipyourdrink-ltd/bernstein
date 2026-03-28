# 625 — Shared Project Context

**Role:** backend
**Priority:** 3 (medium)
**Scope:** large
**Depends on:** none

## Problem

Agents operate in isolation with no shared understanding of the project state. Agent A might modify a file that Agent B is also editing, with neither aware of the other's work. There is no shared knowledge base for cross-agent coordination.

## Design

Implement a tiered shared project context store. Tier 1: private per-agent context (agent's task, assigned files, local decisions). Tier 2: shared project context (file tree with ownership markers, test results, CI status, active modifications registry). Tier 3: persistent knowledge base (architectural decisions, coding conventions, known issues). The shared context is stored in `.sdd/context/` and updated via the task server API. Before an agent starts work, it checks the shared context for conflicts (another agent modifying the same files). The orchestrator updates shared context on every significant event. Implement a simple locking mechanism for files under active modification. Keep the context store lightweight — JSON files with file-level granularity, not line-level.

## Files to modify

- `src/bernstein/core/context_store.py` (new)
- `src/bernstein/core/file_lock.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/task_server.py`
- `tests/unit/test_context_store.py` (new)

## Completion signal

- Agents can read shared project context before starting work
- File ownership tracked and conflicts detected before assignment
- Test results and CI status available in shared context
