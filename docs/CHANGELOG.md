# Changelog

All notable documentation changes are tracked here.

## [1.0.3] — 2026-03-30

### Added
- State-of-the-art CI/CD pipeline: 11 new GitHub Actions workflows
- Three-tier AI PR review (GitHub Models + Gemini CLI + Bernstein deep review)
- Semgrep SAST, license compliance, spelling, dead code analysis, workflow linting
- PR auto-labeling, size warnings, stale cleanup, Dependabot auto-merge
- Release Drafter for automated changelog generation
- Telegram bot notifications on CI completion
- Codecov coverage gating (85% project / 70% patch)
- Concurrency groups on all workflows with cancel-in-progress
- CI and Codecov badges in README

### Changed
- FEATURE_MATRIX updated with CI/CD section (15 new entries)
- GETTING_STARTED expanded with CI pipeline documentation
- Manual backlog index updated with all setup tickets and status tracking

## [1.0.2] — 2026-03-28

### Changed
- Documentation audit: updated outdated model names, CLI references, API endpoints, and GitHub Action version tags
- Default branch references updated from `master` to `main` across all docs

## [1.0.0] — 2026-03-28

### Added
- ACP (Agent Communication Protocol) endpoints for agent interoperability
- A2A (Agent-to-Agent) protocol support
- Cluster mode with multi-node coordination (node registration, heartbeat, status)
- Auth routes: OIDC, SAML, CLI device flow, group mappings, user management
- Graduation system for agent promotion based on performance
- Plans routes for plan listing, approval, and rejection
- Slack integration (slash commands and events)
- Quality dashboard with per-model quality metrics
- Cost history, live cost tracking, and cost alerts endpoints
- File lock tracking via dashboard routes
- Task prioritization, force-claim, and progress reporting endpoints
- Chaos testing CLI group
- Audit CLI group
- Verify CLI command

### Changed
- Version bumped to 1.0.0 (stable release)
- Route modules expanded: acp.py, auth.py, graduation.py, plans.py, slack.py added to core/routes/

## [0.3.0] — 2026-03-28

### Added
- Checkpoint and wrap-up CLI commands for session management
- Task snapshots endpoint for viewing task state history
- Webhook alerts endpoint
- SSE event stream at `/events` for real-time dashboard updates
- Prometheus `/metrics` endpoint for observability
- Bandit-based model routing stats at `/routing/bandit`
- Cache stats endpoint at `/cache-stats`

### Changed
- CLI decomposed further: audit_cmd.py, chaos_cmd.py, checkpoint_cmd.py, verify_cmd.py, wrap_up_cmd.py
- Task server routes expanded with block, progress, and prioritize actions

## [0.2.0] — 2026-03-28

### Added
- Agent discovery system with multi-provider routing (`cli: auto`)
- Quality gates for task verification
- Rule enforcement engine
- Token monitor for real-time usage tracking
- Approval gates for high-risk operations
- MCP server integration
- Hot reload for configuration changes
- Aider, Amp, and Roo Code adapters
- Adapter manager and caching adapter layer
- Environment isolation for adapter processes
- Web dashboard with real-time SSE updates
- Workspace management for multi-repo orchestration
- GitHub App integration for webhook-driven tasks
- Auth middleware and checkpoint commands
- Delegate, trigger, and wrap-up CLI commands

### Changed
- Default CLI adapter is now `auto` (detects installed agents) instead of `claude`
- Test count badge updated: 2500+ to 4250+ (142 test files, 4257 test functions)
- Server decomposed into `core/routes/` (tasks.py, status.py, webhooks.py, costs.py, agents.py, auth.py, dashboard.py, plans.py, quality.py, graduation.py, slack.py)
- Orchestrator decomposed into tick_pipeline.py, task_lifecycle.py, agent_lifecycle.py
- CLI decomposed into helpers.py, run_cmd.py, stop_cmd.py, status_cmd.py, agents_cmd.py, evolve_cmd.py, advanced_cmd.py, and more
- TaskStore extracted to task_store.py with PostgreSQL and Redis backends
- `bernstein catalog` commands renamed to `bernstein agents` (sync, list, validate)
- Adapter listing in DESIGN.md updated to include all current adapters (removed stale kiro.py)
- Example YAML files updated: `cli: claude` changed to `cli: auto`
- All documentation references to `bernstein catalog` updated to `bernstein agents`
- Removed stale "(default)" label from Claude adapter docs (default is now `auto`)

### Architecture (code changes reflected in docs)
- Routes decomposition: core/routes/ with 11 route modules
- Orchestrator decomposition: tick_pipeline.py, task_lifecycle.py, agent_lifecycle.py
- Agent discovery: agent_discovery.py with multi-source detection
- Quality gates: quality_gates.py for pre-merge verification
- Rule enforcement: rule_enforcer.py for policy compliance
- Token monitor: token_monitor.py for real-time token tracking
- Approval system: approval.py for gated operations
- MCP integration: mcp_manager.py and mcp_registry.py
- Cascade router: cascade_router.py for multi-provider routing
- Batch router: batch_router.py for task batching
- Circuit breaker: circuit_breaker.py for provider failure handling
- Semantic cache: semantic_cache.py for prompt deduplication
- Cross-model verifier: cross_model_verifier.py
- Store backends: store_postgres.py, store_redis.py, store_factory.py

## [0.1.0] — 2026-03-28

### Added
- License: Apache 2.0
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
