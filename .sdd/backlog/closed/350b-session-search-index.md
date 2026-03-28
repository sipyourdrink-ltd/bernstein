# 350b — Session Persistence with Full-Text Search
**Role:** backend  **Priority:** 2 (high)  **Scope:** medium

## Problem
HN: "Session identity matters more than session management. If you can't search or resume mid-thought, you're herding cats."

## Design
Index all agent sessions in SQLite FTS5. Search by task title, file touched, error message. Resume any past session. `bernstein sessions search "auth"` → list matching sessions with diffs.


---
**completed**: 2026-03-28 23:57:57
**task_id**: d90b93456964
**result**: Completed: 345 — Interactive Session Streaming (Crystal-killer)
