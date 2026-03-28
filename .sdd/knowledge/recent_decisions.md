# Recent Decisions

No decisions recorded yet.

## [2026-03-28 07:04] [RETRY 2] 413 -- GitHub Pages documentation site (446f33ad0e05)
Completed: [RETRY 2] 413 -- GitHub Pages documentation site. All 6 files present (index.html, getting-started.html, concepts.html, api.html, style.css, script.js). Total size ~81KB. All completion signals verified: path_exists for index.html and style.css, file_contains for Bernstein and viewport.

## [2026-03-28 07:04] [RETRY 1] 413 -- GitHub Pages documentation site (2aaf39e2a499)
Completed: [RETRY 1] 413 -- GitHub Pages documentation site

## [2026-03-28 07:04] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (5db76917ad89)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/

## [2026-03-28 07:04] [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist (ea5cbaf7153b)
Completed: [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist

## [2026-03-28 07:05] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (bd8500147ab5)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop - refactoring already implemented: _fetch_all_tasks() called once per tick, tasks_by_status dict reused by _check_evolve() and all consumers, debug log counting HTTP reads per tick already present

## [2026-03-28 07:05] [RETRY 2] Fix all 31 ruff linting errors across src/bernstein/ (82515d06c867)
Completed: All ruff linting errors already fixed. Verified 0 errors with ruff check and all 1740 tests passing.

## [2026-03-28 07:05] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (c37a1ecda155)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop — already implemented: _fetch_all_tasks() makes single GET /tasks call, tick() uses _tick_http_reads counter for debug logging, _check_evolve() receives tasks_by_status parameter. HTTP reads per tick = 1.

## [2026-03-28 07:06] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (41f809085254)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop — already implemented: _fetch_all_tasks() is called once per tick, tasks_by_status dict passed to all consumers, debug log confirms 1 HTTP read per tick

## [2026-03-28 07:06] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (9e5091db9d10)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 07:07] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (c8f1b61e73db)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 07:09] Evolve cycle 12: test coverage (89f67a9d2472)
Evolve cycle 12 (test coverage): Created 4 QA tasks targeting zero-coverage methods. Current: 1740 tests, 82% coverage. Gaps: _extract_from_agent_log (log parsing, 0 tests), _run_evolution_cycle (auto-evolution, 0 tests), build_manager_description context_files path (0 tests), _check_evolve cycle management (minimal tests). Each task adds 5-8 focused tests for untested business logic.

## [2026-03-28 07:10] Test build_manager_description with context_files (58da4c1e270b)
Completed: Test build_manager_description with context_files

## [2026-03-28 07:10] Test _extract_from_agent_log parsing in orchestrator (33fd3b21abb6)
Completed: Test _extract_from_agent_log parsing in orchestrator

## [2026-03-28 07:10] Test _run_evolution_cycle in orchestrator (cc09d87170da)
Completed: Test _run_evolution_cycle in orchestrator

## [2026-03-28 07:11] Test _check_evolve cycle management logic (9b21398a5bb4)
Completed: Test _check_evolve cycle management logic
