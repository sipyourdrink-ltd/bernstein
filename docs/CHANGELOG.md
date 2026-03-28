# Changelog

All notable documentation changes are tracked here.

## [0.1.0] — 2026-03-28

### Added
- README: new commands (`bernstein ps`, `doctor`, `plugins`, `dashboard`, `trace`, `replay`, `workspace`)
- README: Observability section (process visibility, Prometheus metrics)
- README: Extensibility section (plugin system)
- `docs/competitive-matrix.md` — feature comparison vs CrewAI, AutoGen, LangGraph, etc.
- `docs/zero-lock-in.md` — model-agnostic architecture deep dive
- `docs/CHANGELOG.md` — this file
- `docs/VERSION` — documentation version tracking

### Changed
- README: test count badge updated (2056 → 2415)
- README: CLI commands list expanded with new commands

### Architecture (code changes reflected in docs)
- Process visibility: `bernstein-worker` wrapper, `setproctitle`
- Prometheus `/metrics` endpoint
- Pluggy-based plugin system with 6 hook points
- Isolated test runner (`scripts/run_tests.py`) replacing raw pytest
- CI fixed: removed gitignored force-include, switched to isolated test runner
- Pyright strict: 780 → 0 errors
