# Changelog

All notable project changes are tracked here (code + docs).

## [1.4.11] — 2026-04-03

### Added
- **Bernstein doctor** — comprehensive pre-flight health check: adapters, API keys, ports, `.sdd/` integrity, MCP servers. Auto-repair mode with `--fix`.
- **Per-agent token progress** — real-time token usage tracking per spawned agent, surfaced in `bernstein status`.
- **Context injection token budget** — explicit budgets for injected context (files, lessons, RAG chunks) with graceful truncation and priority ordering.
- **Output style customization** — configurable agent output format via markdown templates.
- **Installation mismatch detection** — detects gaps between expected and installed adapter capabilities.
- **API preconnect warmup** — connection warmup before heavy runs to reduce first-request latency.
- **Worker badge identity** — process identification visible in `bernstein ps` and Activity Monitor.
- **TUI keybinding system** — configurable keyboard shortcuts in the Textual dashboard.
- **Progressive permission prompts** — per-agent permission levels for fine-grained control.
- **Activity tracking metrics** — session-level activity statistics and agent usage patterns.
- **Away summary generation** — summarize what happened while you were away.
- **Commit attribution stats** — per-agent commit statistics.
- **Session analytics** — cumulative insights across runs.
- **Settings snapshot in traces** — agent settings preserved in execution traces.
- **Side question support** — agents can ask clarifying questions mid-task.
- **Diff folding display** — folded diff rendering in agent output.
- **Word-level diff rendering** — character-level change highlighting.
- **Contextual tips system** — in-context hints for agents.
- **Session tag system** — tag and filter runs.
- **Rename session** — session renaming command.
- **Security review command** — `bernstein security-review` for vulnerability assessment.
- **Cumulative progress tracking** — progress tracking across runs.
- **Plugin trust warning** — warns on unverified plugins.
- **Plugin error reporting** — improved error diagnostics for plugin failures.
- **Extra usage provisioning** — additional usage quota management.
- **Truecolor mode detection** — automatic terminal color capability detection.
- **Dirty flag layout caching** — caching optimizations for dirty project detection.
- **Release notes display** — show release notes on startup.

### Fixed
- Context warnings in `bernstein doctor` output for better diagnostics.
- Circuit breaker for repeated compact failures — prevents agent thrashing.

### Changed
- Documentation overhaul: README, GETTING_STARTED, ARCHITECTURE, FEATURE_MATRIX, BENCHMARKS, CHANGELOG, CONTRIBUTING all rewritten against v1.4.11 codebase.

## [1.4.9] — 2026-04-01

### Added
- Process-aware shutdown/drain improvements across CLI and core lifecycle paths.
- Cost analytics enhancements (additional endpoints/aggregation work and routing transparency updates).
- Security enhancements including sensitivity-classification and IP-allowlist related hardening.
- TUI keyboard help (`?`) shortcut support.

### Changed
- Issue triage and documentation alignment pass to reduce roadmap vs shipped-feature drift.
- Retry, lifecycle, and observability narratives updated to better reflect current implementation boundaries.

## [1.4.0] — 2026-03-31

### Added
- **Plan Files**: loadable YAML project plans with stages and steps (`bernstein run plan.yaml`)
- **Server Supervisor**: auto-restart on crash with exponential backoff (max 5 restarts / 10 min)
- **CrashGuard Middleware**: catches unhandled exceptions → 500 instead of process death
- **Orchestrator drain mode**: loop continues while agents are active, even after stop signal
- **Quality gates**: PII scan, mutation testing, benchmark regression detection
- **Gate Runner**: parallel execution of all quality gates (asyncio)
- **Benchmark regression gate**: block merge when performance degrades beyond threshold
- **PII log redaction**: auto-installed filter scrubs emails, phones, SSNs, credit cards from all log output
- **Agent loop detection**: kills agents caught in edit-loop cycles (same file edited N+ times in window)
- **Deadlock detection**: wait-for graph cycle detection with automatic victim selection
- **Cost anomaly detection**: Z-score based cost anomaly signaling with configurable thresholds
- **Per-agent file/command permissions**: role-based matrix restricting which files and commands each role may use
- **Premium visual theme**: CRT power-off effects, gradient splash, block-art logo
- **Live boot log**: orchestrator boot progress shown in Agents panel while no agents spawned
- **Persistent memory**: SQLite-backed cross-session agent memory
- **Context handoff**: structured context briefs for subtask delegation
- **Zero-config mode**: auto-detect project type, no bernstein.yaml required
- **Worktree environment hooks**: auto-symlink node_modules, copy .env
- **FIFO merge queue**: sequential merge with git merge-tree conflict pre-check
- **Ticket Format v1**: YAML frontmatter with model routing, janitor signals, tags
- **10 adapters**: Claude, Codex, Cursor, Gemini, Kiro, OpenCode, Aider, Amp, Roo Code, Generic
- **Futuristic splash screen**: full-screen animated boot sequence
- **Plan display**: mission-briefing style execution plan approval
- **test_cli_run_params.py**: catches cli() → run() parameter sync bugs

### Fixed
- Manager always uses opus/max (was falling back to haiku via fast_path)
- Orchestrator no longer exits while agents still running
- Server failure backoff: 5s per failure instead of constant polling
- Startup crash: missing pii_scan fields in QualityGatesConfig
- .yaml/.md backward compatibility in all backlog parsers

### Changed
- Ticket format migrated from .md to .yaml (YAML frontmatter)
- Version bump 1.3.x → 1.4.0

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
