# Recent Decisions

No decisions recorded yet.

## [2026-03-28 06:09] [RETRY 2] 413 -- GitHub Pages documentation site (a1ffaefe9ced)
Completed: [RETRY 2] 413 -- GitHub Pages documentation site. All 6 files present (index.html, getting-started.html, concepts.html, api.html, style.css, script.js). Total size 81KB. All completion signals pass.

## [2026-03-28 06:10] [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist (bdd9d0675def)
Completed: [RETRY 1] 500 -- Idle agent detection: kill finished agents when open tasks exist

## [2026-03-28 06:10] [RETRY 1] 413 -- GitHub Pages documentation site (70d4f4f22710)
Completed: [RETRY 1] 413 -- GitHub Pages documentation site

## [2026-03-28 06:12] Add unit tests for ManagerAdapter (adapters/manager.py, 0% coverage) (2472ece66910)
Completed: Add unit tests for ManagerAdapter (adapters/manager.py, 0% coverage)

## [2026-03-28 06:12] Evolve cycle 7: test coverage (e65ec40efe27)
Cycle 7 (test coverage): Ran coverage analysis — 80% overall, 1612 tests passing. Identified 4 high-impact gaps and created tasks: (1) ManagerAdapter 0%→90% [2472ece66910], (2) CatalogRegistry.discover 63%→85% [229fbdbb184d], (3) MultiCellOrchestrator tick/reap 66%→85% [24157c665af7], (4) ManagerAgent upgrade/review methods 61%→75% [f3cfc3b14c42]. All tasks target pure/mockable code for reliable test authoring.

## [2026-03-28 06:13] Add tests for CatalogRegistry.discover() and _load_generic_entry() (catalog.py, 63% coverage) (229fbdbb184d)
Completed: Add tests for CatalogRegistry.discover() and _load_generic_entry() (catalog.py, 63% coverage). Created tests/unit/test_catalog_discover.py with 36 tests covering _fetch_from_providers, _load_entry, _load_generic_entry, and _parse_catalog_entry. Coverage improved from 63% to 99%.

## [2026-03-28 06:15] Add tests for MultiCellOrchestrator._tick_cell and _reap_dead_workers (multi_cell.py, 66% coverage) (24157c665af7)
Completed: Add tests for MultiCellOrchestrator._tick_cell and _reap_dead_workers (multi_cell.py, 66% coverage)

## [2026-03-28 06:15] Add tests for ManagerAgent upgrade/review methods (core/manager.py, 61% coverage) (f3cfc3b14c42)
Completed: Add tests for ManagerAgent upgrade/review methods (core/manager.py, 61% coverage)

## [2026-03-28 06:19] Evolve cycle 8: code quality (9852d76fdb3a)
Completed: Evolve cycle 8 planning. Created 4 code quality tasks: (1) Fix 31 ruff linting errors [sonnet/low], (2) Deduplicate is_alive()/kill() across 4 adapters into base class [sonnet/medium], (3) Decompose claude.py spawn() 104-line method + fix resource leak [sonnet/high], (4) Decompose qwen.py spawn()/detect_tier() + fix duplicate import and unsafe getattr [sonnet/high].

## [2026-03-28 06:21] Move duplicated is_alive()/kill() methods to base CLIAdapter class (1c80a22cf7c9)
Completed: Move duplicated is_alive()/kill() methods to base CLIAdapter class

## [2026-03-28 06:22] Decompose qwen.py spawn() and detect_tier() into smaller methods (4be439203cd4)
Completed: Decompose qwen.py spawn() and detect_tier() into smaller methods

## [2026-03-28 06:23] Decompose claude.py spawn() from 104 lines into smaller methods (fbf356c5c902)
Completed: Decompose claude.py spawn() from 104 lines into smaller methods

## [2026-03-28 06:24] Fix all 31 ruff linting errors across src/bernstein/ (4c7f7e9a4fb8)
Completed: Fix all 31 ruff linting errors across src/bernstein/

## [2026-03-28 06:25] [RETRY 2] Fix all 31 ruff linting errors across src/bernstein/ (22316f171108)
Completed: ruff check src/bernstein/ returns 0 errors, all 1717 tests pass.

## [2026-03-28 06:25] [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ (cd4a35e23abe)
Completed: [RETRY 1] Fix all 31 ruff linting errors across src/bernstein/ — ruff check already returns 0 errors and all 1717 tests pass
