# Recent Decisions

No decisions recorded yet.

## [2026-03-28 06:53] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (358750189dac)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ — ruff check already passes with 0 errors, all 1716 tests pass

## [2026-03-28 06:53] [RETRY 1] 413 -- GitHub Pages documentation site (ab4008dacb54)
Completed: [RETRY 1] 413 -- GitHub Pages documentation site

## [2026-03-28 06:53] [RETRY 2] 413 -- GitHub Pages documentation site (9ae79c7d24e6)
Completed: [RETRY 2] 413 -- GitHub Pages documentation site. All 4 pages (index, getting-started, concepts, api) exist with style.css and script.js. Total size 81KB. Responsive, dark mode, syntax highlighting, scroll animations all functional.

## [2026-03-28 06:53] [RETRY 2] Fix all 31 ruff linting errors across src/bernstein/ (7810f12e676e)
Ruff check returns 0 errors. All 1716 tests pass. Linting was already clean — likely fixed by a previous retry.

## [2026-03-28 06:54] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (867065798382)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop

## [2026-03-28 06:54] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (b8cf2bd3055b)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules

## [2026-03-28 06:55] [RETRY 1] Lazy-import Rich dashboard components in CLI modules (41c65ffe93c8)
Completed: [RETRY 1] Lazy-import Rich dashboard components in CLI modules. Verified imports of rich.live.Live, rich.layout.Layout, rich.panel.Panel, rich.table.Table are already lazy (inside functions). CLI startup is ~0.15s, well under 0.5s target. All 1716 tests pass.

## [2026-03-28 06:55] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (6407158f1b61)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop. The tick() method already used _fetch_all_tasks() for a single round-trip. Updated: docstring to reflect single-fetch design, replaced static debug log with a dynamic counter (tick #N: M HTTP read(s) this tick open=X claimed=Y done=Z failed=W). HTTP reads per tick: 1.

## [2026-03-28 06:56] Evolve cycle 11: new features (d2ff2f05abdb)
Cycle 11 (new features): Created 4 tasks — (1) dry-run mode for orchestrator [P1], (2) circular dependency detection in task server [P1], (3) plan export CLI command [P2], (4) live log following CLI command [P2]

## [2026-03-28 06:57] Add circular dependency detection to task server create endpoint (463cadd36182)
Completed: Add circular dependency detection to task server create endpoint

## [2026-03-28 06:57] Add bernstein plan --export command to dump task backlog as reviewable JSON (a3b84bc2f99c)
Completed: Add bernstein plan --export command to dump task backlog as reviewable JSON

## [2026-03-28 07:00] [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop (a6e53ad1696d)
Completed: [RETRY 1] Reduce HTTP round-trips in orchestrator tick loop. Main tick already used _fetch_all_tasks() single-fetch. Extended optimization to _reap_dead_agents(), _refresh_agent_states(), _handle_orphaned_task(), and _retry_or_fail_task() — all now accept tasks_snapshot parameter and resolve from cache instead of making individual GET /tasks/{id} calls. HTTP requests per tick: 1 for normal operation (was 3-4), 1 for reap events (was 1+N per reaped agent). All 1736 tests pass.

## [2026-03-28 07:00] Add bernstein logs --follow command to tail agent output in real-time (46b305f8af8d)
Completed: Add bernstein logs --follow command to tail agent output in real-time

## [2026-03-28 07:02] [RETRY 1] Add --dry-run flag to orchestrator that previews task plan without spawning agents (8f3aa295562d)
Completed: [RETRY 1] Add --dry-run flag to orchestrator that previews task plan without spawning agents

## [2026-03-28 07:02] Add --dry-run flag to orchestrator that previews task plan without spawning agents (3c0273007920)
Completed: Add --dry-run flag to orchestrator that previews task plan without spawning agents
