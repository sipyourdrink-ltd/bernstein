# 527 — Multi-repo orchestration: coordinate work across multiple repositories

**Role:** architect
**Priority:** 3 (medium)
**Scope:** large

## Problem

Real-world projects span multiple repos (frontend, backend, infra, shared libs).
Bernstein currently works within a single repo. Enterprise users need to
orchestrate agents across repo boundaries — e.g., "update the API in repo A
and the client in repo B simultaneously."

## Design

### Workspace concept
- `bernstein.yaml` can define a workspace with multiple repos:
  ```yaml
  workspace:
    repos:
      - path: ./backend
        url: git@github.com:org/backend.git
      - path: ./frontend
        url: git@github.com:org/frontend.git
      - path: ./shared
        url: git@github.com:org/shared-types.git
  ```
- Each repo gets its own `.sdd/` state
- Central workspace `.sdd/` coordinates cross-repo tasks

### Cross-repo task decomposition
- Manager agent sees all repos in context
- Can create tasks that specify target repo
- Agents are spawned in the correct repo directory
- Dependency graph spans repos (e.g., "update shared types" blocks "update frontend client")

### Atomic cross-repo changes
- Related PRs linked via GitHub cross-references
- Option: monorepo-style atomic merge (all PRs merge together or none)
- Rollback: if one PR fails tests, all related PRs are flagged

### Use case: enterprise with 100 repos
- Central Bernstein server coordinates work
- Each repo has a local agent pool
- Cross-cutting refactors (rename API, update schema) propagate automatically

## Files to modify
- `src/bernstein/core/orchestrator.py` — multi-repo awareness
- `src/bernstein/core/spawner.py` — spawn in correct repo
- `bernstein.yaml` — workspace config
- New: `src/bernstein/core/workspace.py` — cross-repo coordination

## Completion signal
- Task in repo A triggers dependent task in repo B
- PRs in both repos reference each other
- `bernstein status` shows cross-repo task graph
