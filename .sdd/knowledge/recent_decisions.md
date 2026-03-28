# Recent Decisions

No decisions recorded yet.

## [2026-03-28 07:25] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (09f944f46aed)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 07:27] Evolve cycle 14: performance (0416d00fa3d3)
Created 4 performance tasks: (1) mtime-cached _compute_total_spent to avoid re-reading JSONL files every tick, (2) server-side status filtering in _fetch_all_tasks to reduce JSON payload, (3) parallel done-task verification using existing ThreadPoolExecutor, (4) write buffering for JSONL appends in TaskStore to reduce file I/O.

## [2026-03-28 07:27] [RETRY 1] Narrow except Exception catches in orchestrator.py (6d360c0b7fcd)
Completed: [RETRY 1] Narrow except Exception catches in orchestrator.py

## [2026-03-28 07:30] Cache _compute_total_spent with mtime-based invalidation (56cd38abae9b)
Completed: Cache _compute_total_spent with mtime-based invalidation

## [2026-03-28 07:33] Add write buffering for JSONL appends in TaskStore (b6491f934ea3)
Completed: Add write buffering for JSONL appends in TaskStore

## [2026-03-28 07:43] [RETRY 2] Use server-side status filtering instead of fetching all tasks (c1d31998811e)
Completed: _fetch_all_tasks already uses server-side status filtering with per-status GET /tasks?status=X calls. All 140 orchestrator tests and 84 server tests pass.

## [2026-03-28 07:44] [RETRY 1] Use server-side status filtering instead of fetching all tasks (4f5bcc17065f)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:44] [RETRY 1] Parallelize done-task verification in _process_completed_tasks (6a82ecdf4607)
Completed: [RETRY 1] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 07:45] [RETRY 1] Parallelize done-task verification in _process_completed_tasks (2645bc46794c)
Completed: [RETRY 1] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 07:45] [RETRY 1] Parallelize done-task verification in _process_completed_tasks (f47349d4c1f6)
Completed: [RETRY 1] Parallelize done-task verification in _process_completed_tasks

## [2026-03-28 07:45] [RETRY 1] Use server-side status filtering instead of fetching all tasks (2c72d5e52a56)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:45] [RETRY 2] Parallelize done-task verification in _process_completed_tasks (d75f22ece52f)
Completed: [RETRY 2] Parallelize done-task verification in _process_completed_tasks — verified existing implementation: verify_task() calls fan out via self._executor (ThreadPoolExecutor max_workers=4), results collected after all complete. Two parallel verification tests pass. All 140 tests pass.

## [2026-03-28 07:45] [RETRY 2] Use server-side status filtering instead of fetching all tasks (acc75ae24b88)
Completed: Server-side status filtering already implemented. _fetch_all_tasks() makes per-status filtered GET /tasks?status=X calls (line 168) instead of unfiltered fetch. Defaults to [open, claimed, done, failed]. All 224 tests pass.

## [2026-03-28 07:48] [RETRY 1] Use server-side status filtering instead of fetching all tasks (04ca90131cad)
Completed: [RETRY 1] Use server-side status filtering instead of fetching all tasks

## [2026-03-28 07:48] [RETRY 2] Use server-side status filtering instead of fetching all tasks (1b9da3b00886)
Completed: [RETRY 2] Use server-side status filtering instead of fetching all tasks. Implementation already in place: _fetch_all_tasks accepts optional statuses list, makes one GET /tasks?status=X per status (defaulting to open,claimed,done,failed). All 224 tests pass.
