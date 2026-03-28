# Recent Decisions

No decisions recorded yet.

## [2026-03-28 06:45] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (e9a8a959f73f)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:46] [RETRY 1] 413 -- GitHub Pages documentation site (12df53e17b44)
Completed: [RETRY 1] 413 -- GitHub Pages documentation site

## [2026-03-28 06:47] [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist (5b97c90f52cb)
Completed: [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist

## [2026-03-28 06:47] [RETRY 2] 413 -- GitHub Pages documentation site (ea3a1e4a4968)
Completed: [RETRY 2] 413 -- GitHub Pages documentation site. All 6 files exist (index.html, getting-started.html, concepts.html, api.html, style.css, script.js), total size 81KB under 100KB limit, all completion signals verified.

## [2026-03-28 06:48] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (d44431eadf14)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:48] [RETRY 2] Fix all 31 ruff linting errors across src/bernstein/ (27f9e1d21481)
Fixed all 10 ruff linting errors: 3 F821 (undefined name) in cli/main.py by quoting return type annotations, 5 E402 (import not at top) in core/spawner.py by moving runtime imports above function definitions, 2 UP037 auto-fixed by ruff. All 1716 tests pass.

## [2026-03-28 06:48] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (d510249035d7)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/

## [2026-03-28 06:49] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (fb4edba76104)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop. The refactor was already in place: tick() calls _fetch_all_tasks() once per tick (line 376), reuses tasks_by_status dict throughout, passes it to _check_evolve() and all consumers. Debug log on line 382 confirms 1 HTTP fetch per tick. _fetch_tasks() (the old per-status fetcher) is still defined but unused in the tick path.

## [2026-03-28 06:49] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (facd88dac369)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 06:49] Evolve cycle 10: documentation (03f8a023ded1)
Created 3 documentation tasks: (1) Fix stale README test badge 1598→1716 and verify CLI commands, (2) Unhide init and live CLI commands to match GETTING_STARTED.md, (3) Add manual testing workflow to CONTRIBUTING.md for contributors.

## [2026-03-28 06:50] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (6e4ca9bdc03e)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 06:50] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (e4eb1c6bada1)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:50] Unhide init and live CLI commands so they match documentation (db86c0d2abe8)
Completed: Unhide init and live CLI commands so they match documentation

## [2026-03-28 06:51] Update README.md: fix stale test count and verify CLI commands (0e5cbb41df74)
Completed: Update README.md: fix stale test count and verify CLI commands

## [2026-03-28 06:51] Add manual testing workflow to CONTRIBUTING.md (89c93cddd788)
Completed: Add manual testing workflow to CONTRIBUTING.md
