# Changelog

All notable documentation changes are tracked here.

## [0.1.0] — 2026-03-28

### Added
- License switched from PolyForm Noncommercial to Apache 2.0
- Per-run cost budgeting (`--budget 5.00`) with threshold warnings
- CI auto-fix pipeline with GitHub Actions log parser
- GitHub Action (`action.yml`) for CI-triggered orchestration
- MCP tool access — agents use MCP servers (stdio/SSE)
- TUI session manager (`bernstein live`) with Textual
- "The Bernstein Way" architecture tenets document
- Quickstart demo (`examples/quickstart/`)
- Comparison pages (`docs/compare/`)
- GitHub Action documentation (`docs/github-action.md`)
- Feature cards for cost budgeting, GitHub Action, MCP on index page
- `docs/competitive-matrix.md` — feature comparison vs CrewAI, AutoGen, LangGraph, etc.
- `docs/zero-lock-in.md` — model-agnostic architecture deep dive
- `docs/CHANGELOG.md` — this file
- `docs/VERSION` — documentation version tracking

### Changed
- All license references updated to Apache 2.0 across all HTML and markdown docs
- README: quickstart section with full install → init → run flow
- README: test count badge, license badge, benchmark badge
- Getting Started: fixed test command to use isolated runner
- Comparison table: added cost budgeting and GitHub Action rows

### Architecture (code changes reflected in docs)
- CostTracker with BudgetStatus and GET /costs/{run_id} endpoint
- CIFixPipeline with CILogParser protocol and GitHubActionsParser
- MCPManager for server lifecycle (start/stop/health check)
- Textual TUI app with TaskListWidget, AgentLogWidget, StatusBar
- Process visibility: `bernstein-worker` wrapper, `setproctitle`
- Prometheus `/metrics` endpoint
- Pluggy-based plugin system with 6 hook points
- Isolated test runner (`scripts/run_tests.py`) replacing raw pytest
- Pyright strict: 780 → 0 errors
