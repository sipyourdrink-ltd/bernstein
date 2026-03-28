# Recent Decisions

No decisions recorded yet.

## [2026-03-28 07:56] [RETRY 1] Use server-side status filtering instead of fetching all tasks (22850454c089)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:56] [RETRY 1] Parallelize done-task verification in _process_completed_tasks (2d55588d591d)
Completed: [RETRY 1] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 07:56] [RETRY 1] Use server-side status filtering instead of fetching all tasks (c85d95cc8917)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:58] [RETRY 1] Use server-side status filtering instead of fetching all tasks (8aee2041bf4b)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:58] [RETRY 2] Parallelize done-task verification in _process_completed_tasks (542f5325b748)
Completed: [RETRY 2] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 07:58] [RETRY 2] Use server-side status filtering instead of fetching all tasks (857f0bb5e151)
Completed: _fetch_all_tasks now makes per-status GET /tasks?status=X calls instead of one unfiltered GET /tasks. All 1778 tests pass.
## [2026-03-28 07:59] Fix CONTRIBUTING.md reference to nonexistent bernstein serve command (6d403064f183)
Completed: Fix CONTRIBUTING.md reference to nonexistent bernstein serve command

## [2026-03-28 07:59] Add missing CLI subcommands to GETTING_STARTED.md reference table (e318b85ad557)
Completed: Add missing CLI subcommands to GETTING_STARTED.md reference table

## [2026-03-28 07:59] [RETRY 2] Use server-side status filtering instead of fetching all tasks (6cbaa6daa9f8)
Completed: [RETRY 2] Use server-side status filtering instead of fetching all tasks — _fetch_all_tasks now makes per-status GET /tasks?status=X calls instead of one unfiltered GET /tasks. Updated caller comment and HTTP read counter. All 140 orchestrator tests pass.

## [2026-03-28 07:59] Update DESIGN.md implementation plan to reflect current state (4a9e3857ab4e)
Completed: Update DESIGN.md implementation plan to reflect current state

## [2026-03-28 08:00] [RETRY 2] Use server-side status filtering instead of fetching all tasks (19b8e8936544)
Completed: [RETRY 2] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 08:01] [RETRY 1] Parallelize done-task verification in _process_completed_tasks (4b3bd6401307)
Completed: [RETRY 1] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 08:01] Update README.md test count and verify CLI usage (af8e8c7ce062)
Completed: Update README.md test count and verify CLI usage

## [2026-03-28 08:02] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (65a1c650fceb)
Already implemented: tick loop makes 1 GET /tasks per tick via _fetch_all_tasks(), buckets client-side, passes tasks_by_status to _check_evolve() and all consumers. Debug log already present counting requests per tick.

## [2026-03-28 08:03] [RETRY 2] Reduce HTTP round-trips in orchestrator tick loop (11989050c702)
Verified: tick loop already makes 1 HTTP read via _fetch_all_tasks (single GET /tasks, client-side bucketing). Fixed stale docstring that incorrectly claimed per-status queries. Fallback GET /tasks/{id} calls in _handle_orphaned_task and _retry_or_fail_task only fire on cache misses (edge case). Debug log counting HTTP reads per tick already present at line 420. 140 tests pass.

## [2026-03-28 08:15] Fix _fetch_all_tasks to include failed status (ccdc20f11608)
Completed: Fix _fetch_all_tasks to include failed status

## [2026-03-28 08:15] Plan and decompose goal into tasks (efd3d3fb1429)
Analyzed codebase (1309 tests passing, 1 failing). Identified 5 gaps between DESIGN.md and implementation. Created 5 tasks: (1) P1 fix _fetch_all_tasks missing failed status [ccdc20f11608], (2) P2 wire WorktreeManager into spawner [2e6a8ef5149e], (3) P2 wire MultiCellOrchestrator into bootstrap [d41f98014a9b], (4) P3 implement run retrospectives [d229e17fcf51], (5) P2 lifecycle integration test [1e43f5102645]. Tasks ordered by priority with dependency on bug fix.

## [2026-03-28 08:16] Wire WorktreeManager into AgentSpawner (2e6a8ef5149e)
Completed: Wire WorktreeManager into AgentSpawner

## [2026-03-28 08:18] Add integration test for spawn-execute-complete lifecycle (1e43f5102645)
Completed: Add integration test for spawn-execute-complete lifecycle

## [2026-03-28 08:19] Implement run retrospective report generation (d229e17fcf51)
Completed: Implement run retrospective report generation

## [2026-03-28 08:20] Wire MultiCellOrchestrator into bootstrap and CLI (f16f8154f735)
Completed: Wire MultiCellOrchestrator into bootstrap and CLI
