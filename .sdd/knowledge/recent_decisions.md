# Recent Decisions

No decisions recorded yet.

## [2026-03-28 06:26] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (cd4a35e23abe)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ — ruff check already returns 0 errors and all 1717 tests pass

## [2026-03-28 06:27] [RETRY 1] 413 -- GitHub Pages documentation site (b2b835b00f4e)
Completed: [RETRY 1] 413 -- GitHub Pages documentation site

## [2026-03-28 06:27] [RETRY 2] 413 -- GitHub Pages documentation site (e3335db75555)
Completed: [RETRY 2] 413 -- GitHub Pages documentation site. All 6 files present (index.html, getting-started.html, concepts.html, api.html, style.css, script.js) at ~81KB total. All completion signals verified: path_exists for index.html and style.css, file_contains for Bernstein and viewport.

## [2026-03-28 06:27] [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist (6896739fad29)
Completed: [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist

## [2026-03-28 06:28] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (63f783be890b)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ — ruff check returns 0 errors, all 1717 tests pass

## [2026-03-28 06:28] [RETRY 2] Fix all 31 ruff linting errors across src/bernstein/ (8df9e87fcce8)
Completed: ruff check src/bernstein/ returns 0 errors, all 1717 tests pass. Linting errors were already fixed by a previous retry.

## [2026-03-28 06:30] Evolve cycle 9: performance (ab86024d3edc)
Created 4 performance tasks: (1) Reduce HTTP round-trips in orchestrator tick from 3-4 to 1-2 calls [39ee144b563d], (2) heapq priority queue for O(log n) task claiming [d8cb86cb4f05], (3) Cache template/config file reads in spawner [8800c2977370], (4) Lazy-import Rich dashboard components for faster CLI startup [eeae4f93865b]

## [2026-03-28 06:31] Use heapq priority queue for task claim_next() in TaskStore (d8cb86cb4f05)
Completed: Use heapq priority queue for task claim_next() in TaskStore

## [2026-03-28 06:31] Cache template and config file reads in spawner (8800c2977370)
Completed: Cache template and config file reads in spawner

## [2026-03-28 06:34] Lazy-import Rich dashboard components in CLI modules (46bb9b7449ac)
Completed: Lazy-import Rich dashboard components in CLI modules — all Rich dashboard imports (Live, Layout, Panel, Table) are already lazy inside functions. Only Console remains at module level, which is correct as it is lightweight and used broadly.

## [2026-03-28 06:35] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (186c1441fc8b)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 06:43] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (d23dc84393fc)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 06:44] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (1e376affda31)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:44] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (e9a8a959f73f)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:44] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (9cb651425c09)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop
