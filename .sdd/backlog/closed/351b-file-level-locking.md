# 351b — File-Level Locking System
**Role:** backend  **Priority:** 1 (critical)  **Scope:** small

## Problem
Stoneforge: "File locking makes conflicts structurally impossible." Currently agents can edit same files.

## Design
Agents declare owned files at spawn (from task.owned_files or auto-detected). Orchestrator maintains lock table. Other agents see lock → work on something else. Locks release on task completion.


---
**completed**: 2026-03-28 23:53:57
**task_id**: 5335cd115962
**result**: Completed: 351b — File-Level Locking System. Implemented FileLockManager with persist-to-disk, TTL expiry, acquire/release/check_conflicts API. Wired into orchestrator._check_file_overlap, task_lifecycle._claim_file_ownership, and agent_lifecycle._release_file_ownership. 23 tests passing.
