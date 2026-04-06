# Bernstein 200-Ticket Strategic Roadmap (P0-P3)

## Context

This roadmap defines 200 NEW strategic tickets that complement the existing 300-task plan (strategic-300-tasks.md). Zero overlap. These tickets address gaps in the original plan identified through:
- 2025-2026 competitive landscape shifts (Claude Agent SDK, MCP 2026 roadmap, OpenAI Agents SDK, JetBrains ACP)
- Enterprise adoption blockers surfaced by Deloitte, Gartner, and IBM agentic AI analyses
- Multi-agent observability maturation (LangFuse, Maxim AI, Arize)
- Contextual bandit research for LLM routing (BaRP framework, Thompson Sampling production results)
- MCP protocol evolution (Streamable HTTP, MCP Server Cards, Linux Foundation governance)
- Agent-to-Agent protocol (A2A) and Agent Client Protocol (ACP) standardization
- The "100 agents on a monorepo" scaling question
- The "debug a 50-agent orchestration that produced wrong code" question

The existing 300 tasks cover: orchestrator hardening, task lifecycle, agent spawning, permissions, hooks, TUI, web dashboard, Claude Code integration, MCP basics, testing, CLI UX, configuration, cost tracking, documentation, and enterprise basics.

This roadmap covers TEN new strategic dimensions that go BEYOND the existing plan.

## Distribution Summary

| # | Dimension | p0 | p1 | p2 | p3 | Total |
|---|-----------|----|----|----|----|-------|
| 1 | Product-Market Fit | 3 | 6 | 8 | 3 | 20 |
| 2 | Developer Experience | 2 | 6 | 8 | 4 | 20 |
| 3 | Reliability Engineering | 3 | 5 | 8 | 4 | 20 |
| 4 | Intelligence Layer | 2 | 5 | 8 | 5 | 20 |
| 5 | Ecosystem & Partnerships | 2 | 5 | 8 | 5 | 20 |
| 6 | Platform Architecture | 2 | 5 | 8 | 5 | 20 |
| 7 | Security & Compliance | 2 | 5 | 8 | 5 | 20 |
| 8 | Observability & Analytics | 1 | 5 | 8 | 6 | 20 |
| 9 | Agent Quality | 2 | 4 | 8 | 6 | 20 |
| 10 | Future-proofing | 1 | 4 | 8 | 7 | 20 |
| | **Totals** | **20** | **50** | **80** | **50** | **200** |

**Complexity distribution**: c1 (small): ~55 | c2 (medium): ~85 | c3 (large): ~60

---

## Dimension 1: Product-Market Fit (20 tickets)

What makes an enterprise BUY Bernstein instead of CrewAI, LangGraph, or building their own? What makes a 10-person startup choose Bernstein over single-agent workflows? These tickets address the gap between "cool open-source project" and "line item on a procurement order."

### ROAD-001: One-command onboarding that produces a working multi-agent run in under 60 seconds
**Priority**: p0 | **Complexity**: c2 | **Category**: Product-Market Fit
`bernstein quickstart` should clone a sample repo, detect the language, auto-discover installed CLI agents, generate a minimal plan, execute it, and produce a visible diff -- all in one command with zero configuration. The existing `quickstart_cmd.py` generates config but never runs anything. This is the single most important conversion moment.

### ROAD-002: Competitive benchmark suite comparing Bernstein vs CrewAI vs LangGraph on identical tasks
**Priority**: p0 | **Complexity**: c3 | **Category**: Product-Market Fit
Build a reproducible benchmark suite that runs the same 10 coding tasks (bug fix, feature add, refactor, test generation, migration, docs, security audit, performance optimization, dependency upgrade, multi-file feature) across Bernstein, CrewAI, and LangGraph. Measure: completion rate, code quality (lint/type-check pass rate), cost, wall-clock time, and lines of correct code per dollar. Publish results and methodology. This is the "show me the numbers" asset every buyer needs.

### ROAD-003: Pre-built "solution packs" for top 5 enterprise use cases
**Priority**: p0 | **Complexity**: c2 | **Category**: Product-Market Fit
Package ready-to-run plan files + role templates + quality gates for: (1) legacy code migration (Java 8 to 17, Python 2 to 3), (2) test coverage expansion, (3) security vulnerability remediation, (4) documentation generation from code, (5) dependency upgrade campaigns. Each pack includes a README, expected outcomes, and cost estimates. Enterprises buy solutions, not frameworks.

### ROAD-004: ROI calculator CLI that estimates time/cost savings from Bernstein adoption
**Priority**: p1 | **Complexity**: c2 | **Category**: Product-Market Fit
`bernstein roi --repo . --engineers 8 --hourly-rate 85` analyzes the repo (size, language, test coverage, open issues) and estimates: how many agent-hours Bernstein would save per sprint, projected cost vs. engineer cost, and payback period. Outputs a shareable report. This is the justification artifact for procurement.

### ROAD-005: "Bernstein vs. Single Agent" A/B comparison mode
**Priority**: p1 | **Complexity**: c3 | **Category**: Product-Market Fit
Run the same plan twice: once with full multi-agent orchestration, once with a single sequential agent. Compare: wall-clock time, cost, code quality, merge conflicts. Auto-generate a comparison report. This lets potential users see the multi-agent advantage on their own codebase, not a demo repo.

### ROAD-006: Embeddable status widget for GitHub PR descriptions
**Priority**: p1 | **Complexity**: c1 | **Category**: Product-Market Fit
When Bernstein creates PRs, embed a status badge/widget showing: agents used, cost, quality gate results, and time-to-completion. This makes Bernstein's value visible to every code reviewer, driving organic adoption within engineering teams.

### ROAD-007: Team adoption dashboard showing Bernstein usage metrics across an org
**Priority**: p1 | **Complexity**: c2 | **Category**: Product-Market Fit
Aggregate usage across team members: total runs, tasks completed, cost saved (vs. estimate), code merged, quality gate pass rate. Expose at `/dashboard/team` for engineering managers to track adoption and value.

### ROAD-008: Curated example gallery with 20+ real-world orchestration patterns
**Priority**: p1 | **Complexity**: c2 | **Category**: Product-Market Fit
Maintain a `examples/` directory with working plans for: monorepo multi-service feature, database migration with rollback, API versioning, microservice extraction, i18n rollout, compliance remediation, performance optimization campaign, tech debt sprint, onboarding codebase documentation, and more. Each example is a self-contained plan that runs against a provided sample repo.

### ROAD-009: Free-tier cloud-hosted Bernstein for evaluation (no install required)
**Priority**: p1 | **Complexity**: c3 | **Category**: Product-Market Fit
Host a sandboxed Bernstein instance where prospects can paste a GitHub URL, select a solution pack, and watch agents work on their repo. Limited to 3 agents, $2 budget, public repos only. Eliminates the "I don't have time to install it" objection.

### ROAD-010: Integration with GitHub Issues/Jira for automatic ticket-to-task mapping
**Priority**: p2 | **Complexity**: c2 | **Category**: Product-Market Fit
`bernstein sync github --repo owner/repo --label bernstein` pulls open issues with a specific label, converts them to Bernstein tasks, executes them, and posts PRs back to the issues. Same for Jira. This is how Bernstein fits into existing workflows rather than replacing them.

### ROAD-011: "Works with your stack" compatibility matrix auto-detection
**Priority**: p2 | **Complexity**: c2 | **Category**: Product-Market Fit
On first run, analyze the repo and report: which languages are supported, which frameworks have pre-built quality gates, which CI systems have integration templates, and any unsupported components. Show a compatibility score (e.g., "92% compatible -- only Terraform files lack quality gates").

### ROAD-012: Customer success telemetry (opt-in) for product-led growth analytics
**Priority**: p2 | **Complexity**: c2 | **Category**: Product-Market Fit
With explicit opt-in, collect anonymized usage telemetry: run frequency, agent count, task types, completion rates, error categories. This data drives product prioritization and lets the team know which features are actually used.

### ROAD-013: White-label mode for consultancies and platform teams
**Priority**: p2 | **Complexity**: c2 | **Category**: Product-Market Fit
Allow platform teams and consultancies to rebrand Bernstein: custom CLI name, custom TUI branding, custom dashboard logo, custom role templates. `bernstein.yaml` gets a `branding:` section. This opens the channel sales motion.

### ROAD-014: Outcome-based pricing model with cost-per-successful-task metering
**Priority**: p2 | **Complexity**: c3 | **Category**: Product-Market Fit
Instead of seat-based or usage-based pricing alone, implement metering for "cost per successful task completion" -- charge only when quality gates pass and code merges. This aligns Bernstein's revenue with customer outcomes and is a strong differentiator against frameworks that charge regardless of success.

### ROAD-015: Localization of CLI output and documentation for top 5 languages
**Priority**: p2 | **Complexity**: c2 | **Category**: Product-Market Fit
Add i18n support for CLI messages, error text, and documentation in Japanese, Korean, Mandarin, German, and Portuguese. The agent orchestration market is global; English-only limits TAM significantly.

### ROAD-016: Migration wizard from CrewAI and LangGraph with automated conversion
**Priority**: p2 | **Complexity**: c3 | **Category**: Product-Market Fit
`bernstein migrate --from crewai --input crew.py` parses a CrewAI crew definition and generates an equivalent Bernstein plan.yaml + role templates. Same for LangGraph workflow definitions. Go beyond the existing migration docs (DOC-011) to provide automated tooling, not just a guide.

### ROAD-017: Case study generator that produces a shareable report from each run
**Priority**: p2 | **Complexity**: c1 | **Category**: Product-Market Fit
After each successful run, `bernstein report --format html` generates a polished, shareable report: task summary, agent timeline visualization, code quality metrics, cost breakdown, and before/after comparison. Teams use these to justify continued investment.

### ROAD-018: Multi-model cost arbitrage across 10+ providers simultaneously
**Priority**: p3 | **Complexity**: c3 | **Category**: Product-Market Fit
Route tasks across Anthropic, OpenAI, Google, Mistral, Deepseek, Alibaba, and local models simultaneously, choosing the cheapest provider that meets quality thresholds per task. As model commoditization accelerates, cost arbitrage becomes the primary value proposition for non-differentiated tasks.

### ROAD-019: "Bernstein for Education" tier with classroom orchestration features
**Priority**: p3 | **Complexity**: c2 | **Category**: Product-Market Fit
A free tier for CS education: students run multi-agent coding exercises, instructors review orchestration patterns, and the system provides explanations of agent decisions. Builds pipeline of future enterprise users familiar with the platform.

### ROAD-020: Vertical-specific agent packs (FinTech compliance, HealthTech HIPAA, GovTech FedRAMP)
**Priority**: p3 | **Complexity**: c3 | **Category**: Product-Market Fit
Pre-configured agent packs with industry-specific quality gates, compliance checks, and role templates. FinTech pack includes PCI-DSS code scanning; HealthTech includes PHI detection; GovTech includes STIG compliance checks. These command premium pricing.

---

## Dimension 2: Developer Experience (20 tickets)

What makes a developer say "I can't go back to single-agent"? What reduces the time from "install" to "aha moment" to under 5 minutes? These tickets focus on the inner loop: write plan, run, debug, iterate.

### ROAD-021: Interactive plan builder with real-time validation and preview
**Priority**: p0 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein plan build` launches an interactive TUI where developers define tasks, set dependencies, assign roles, and see a live dependency graph + cost estimate. Validates as they type: cycle detection, file overlap warnings, model availability. Currently, plan creation is a raw YAML editing experience with no feedback until runtime.

### ROAD-022: Agent replay debugger that steps through agent decisions post-mortem
**Priority**: p0 | **Complexity**: c3 | **Category**: Developer Experience
`bernstein debug <run-id> --agent <session-id>` opens a TUI that replays the agent's decision sequence: each tool call, each file read, each edit, each test run. User can step forward/backward, inspect state at each point, and annotate where the agent went wrong. This is the "time-travel debugger" for agent orchestrations.

### ROAD-023: Live agent log multiplexer in terminal (tmux-style split view)
**Priority**: p1 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein watch` splits the terminal into N panes (one per active agent) showing live streaming output. Users see all agents working simultaneously, like watching a team of developers. Different from TUI-008 (which is a widget within Textual) -- this is a standalone tmux-like experience for terminals that do not support Textual.

### ROAD-024: "Why did this fail?" natural language explanation for every task failure
**Priority**: p1 | **Complexity**: c2 | **Category**: Developer Experience
When a task fails, `bernstein explain <task-id>` generates a plain-English explanation: "Task TASK-42 failed because the agent modified auth.py which is outside its allowed scope (backend/api/). The permission matrix for the backend role restricts writes to backend/**. Suggested fix: expand the task scope or split into two tasks." Uses the agent log, quality gate results, and permission traces to construct the explanation.

### ROAD-025: Inline diff annotation showing which agent changed which line
**Priority**: p1 | **Complexity**: c1 | **Category**: Developer Experience
In `bernstein diff` and in PR descriptions, annotate each changed line with the agent session that produced it. When reviewing a multi-agent PR, reviewers can see "lines 42-67 by backend-agent-3, lines 102-110 by qa-agent-1" instead of a monolithic diff.

### ROAD-026: Plan template library with "fork and customize" workflow
**Priority**: p1 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein plan templates` lists community-contributed plan templates. `bernstein plan fork <template-name>` copies a template into the project and opens it for customization. Backed by a Git-hosted template registry that accepts contributions.

### ROAD-027: Real-time cost ticker in terminal status bar during runs
**Priority**: p1 | **Complexity**: c1 | **Category**: Developer Experience
Display a running cost counter in the terminal status bar (using ANSI escape sequences, no TUI required): "$2.34 spent | 3 agents active | 7/12 tasks done | ETA 4m". Visible even when output is scrolling. Different from COST-003 (which is TUI sidebar) and TUI-006 (which is TUI sparkline) -- this is for raw terminal users.

### ROAD-028: Git blame integration showing Bernstein run provenance per line
**Priority**: p1 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein blame <file>` extends git blame with: which Bernstein run produced each line, which agent, which task, and the task description. `bernstein blame --since <run-id>` scopes to a specific run. This answers "why was this code written?" months after the run.

### ROAD-029: Autocomplete and validation for plan YAML in VS Code and JetBrains
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
Publish a JSON Schema for plan YAML files and provide VS Code / JetBrains extensions that offer: autocomplete for role names, complexity levels, scope patterns; inline validation; hover documentation for each field. Different from CFG-015 (which covers bernstein.yaml config schema) -- this is specifically for plan files.

### ROAD-030: Interactive task dependency graph in the browser with zoom/filter
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein graph --web` opens a browser-based interactive dependency graph (using D3.js or Cytoscape.js) with: zoom, pan, filter by status/role/priority, click-to-inspect task details, and critical path highlighting. Different from TASK-019 (ASCII CLI graph) and TUI-015 (TUI Gantt) -- this is a full web visualization.

### ROAD-031: "Bernstein Playground" local sandbox for experimenting without affecting the repo
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein playground` creates a temporary clone, runs agents in it, and shows the result as a diff against the original. If the user likes the result, `bernstein playground apply` merges it. If not, `bernstein playground discard` cleans up. Zero-risk experimentation.

### ROAD-032: Smart error autocorrect that suggests and applies fixes for common plan mistakes
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
When a plan fails validation, offer to fix it: "Role 'backnd' not found. Did you mean 'backend'? [Y/n]". Auto-fix typos in role names, model names, file paths. Offer to add missing dependencies when cycle-free ordering is possible.

### ROAD-033: Agent conversation inspector showing full LLM interaction per task
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein inspect <task-id>` shows the full LLM conversation: system prompt, user messages, assistant responses, tool calls, tool results. Syntax-highlighted, searchable, with token counts per message. Goes beyond CLAUDE-019 (which exports to file) -- this is an interactive inspector.

### ROAD-034: Plan diff command showing what changed between two plan versions
**Priority**: p2 | **Complexity**: c1 | **Category**: Developer Experience
`bernstein plan diff v1.yaml v2.yaml` shows: tasks added, tasks removed, tasks modified, dependency changes, scope changes. Structured output with color coding. Useful when iterating on plans or reviewing plan PRs.

### ROAD-035: Contextual help system that shows relevant docs inline with errors
**Priority**: p2 | **Complexity**: c1 | **Category**: Developer Experience
When any error occurs, append a clickable link to the relevant section of the troubleshooting guide (DOC-001). "Error: spawn rate limit exceeded. See: https://bernstein.dev/docs/troubleshooting#rate-limits". Different from CLI-001 (which provides next-step suggestions) -- this links to full documentation.

### ROAD-036: Agent performance leaderboard showing which models/adapters excel at which task types
**Priority**: p2 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein leaderboard` shows historical performance: "For test-generation tasks, Sonnet 4 completes 92% at $0.08 avg vs Opus 4 at 97% for $0.41 avg." Helps developers pick the right model for each role. Different from COST-012 (which recommends downgrades post-run) -- this is a persistent, cross-run leaderboard.

### ROAD-037: Notification integration with macOS/Linux desktop notifications
**Priority**: p2 | **Complexity**: c1 | **Category**: Developer Experience
Send native OS notifications when: run completes, task fails, budget threshold reached, merge conflict detected. Uses `osascript` on macOS, `notify-send` on Linux. Different from existing hook-based notifications (HOOK-003/013) -- this is zero-config, works out of the box.

### ROAD-038: "Explain this plan" command that describes what a plan will do in plain English
**Priority**: p3 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein plan explain my-plan.yaml` uses an LLM to generate a human-readable summary: "This plan will migrate the authentication module from session-based to JWT-based auth in 4 stages: first updating the User model, then the middleware, then the API endpoints, and finally the tests. It will use 3 backend agents and 1 QA agent, estimated cost $4-8." Useful for plan review by non-technical stakeholders.

### ROAD-039: Voice-controlled orchestration via system microphone
**Priority**: p3 | **Complexity**: c3 | **Category**: Developer Experience
"Hey Bernstein, run the test coverage plan on the payments service" triggers plan execution via voice. Uses local speech-to-text (Whisper) and intent parsing. Hands-free orchestration for developers who are reviewing code on another screen.

### ROAD-040: Collaborative plan editing with real-time sync between team members
**Priority**: p3 | **Complexity**: c3 | **Category**: Developer Experience
Multiple developers can edit the same plan simultaneously (CRDT-based sync), see each other's cursors, and resolve conflicts in real-time. Like Google Docs for orchestration plans. Requires the hosted Bernstein server from ROAD-009.

### ROAD-041: AI-powered plan generator from natural language description
**Priority**: p3 | **Complexity**: c2 | **Category**: Developer Experience
`bernstein plan generate "Add rate limiting to all API endpoints with Redis backing"` analyzes the repo, identifies relevant files, generates a multi-stage plan with appropriate roles and dependencies, and estimates cost. Different from the existing planner (which uses internal LLM for task decomposition) -- this generates entire plans from a one-line description.

---

## Dimension 3: Reliability Engineering (20 tickets)

What breaks when 100 agents run simultaneously on a monorepo? What happens during a 3-hour orchestration when the network flaps? These tickets address failure modes that only surface at scale or over extended time periods.

### ROAD-042: Git lock contention resolver for concurrent worktree operations
**Priority**: p0 | **Complexity**: c2 | **Category**: Reliability Engineering
When 10+ agents operate on worktrees of the same repo, `git` lock contention on `.git/index.lock` causes cascading failures. Implement a lock-aware git operation queue that serializes conflicting operations (fetch, merge, gc) while allowing parallel reads. Add exponential backoff for lock acquisition with diagnostic logging.

### ROAD-043: Filesystem inode/disk exhaustion detection and preemptive response
**Priority**: p0 | **Complexity**: c2 | **Category**: Reliability Engineering
Each agent worktree consumes inodes and disk space. At 50+ worktrees, filesystems can hit inode limits (ext4) or run low on disk. Add a resource monitor that checks inode usage and free disk before each spawn, pauses spawning at 85% utilization, and triggers worktree cleanup at 90%. Different from the memory guard (ORCH-011) -- this is filesystem-specific.

### ROAD-044: Network partition detector with split-brain prevention for cluster mode
**Priority**: p0 | **Complexity**: c3 | **Category**: Reliability Engineering
In cluster mode, a network partition can cause two nodes to both claim the same task. Implement a partition detector using heartbeat quorum: a node must receive heartbeats from >50% of registered nodes to continue claiming tasks. Partitioned nodes enter read-only mode until the partition heals.

### ROAD-045: Merge conflict auto-resolution for non-overlapping semantic changes
**Priority**: p1 | **Complexity**: c3 | **Category**: Reliability Engineering
When two agents modify the same file but in non-overlapping sections, git reports a merge conflict but the changes are semantically independent. Build a semantic merge resolver that: detects non-overlapping hunks, verifies they do not interact (no shared variable references), and auto-resolves. Fall back to human review for overlapping changes.

### ROAD-046: Agent process watchdog with resource consumption tracking per PID
**Priority**: p1 | **Complexity**: c2 | **Category**: Reliability Engineering
Monitor each agent process's CPU, memory, open file descriptors, and child process count. Kill agents that exceed configurable thresholds: >4GB RSS, >90% CPU for >5min, >1000 open fds. Different from AGENT-013 (which sets OS-level limits at spawn) -- this is continuous runtime monitoring with granular thresholds.

### ROAD-047: WAL compaction for long-running orchestrations
**Priority**: p1 | **Complexity**: c2 | **Category**: Reliability Engineering
For orchestrations running 3+ hours with thousands of ticks, the WAL grows unboundedly. Implement WAL compaction that: checkpoints committed entries, truncates the WAL to only uncommitted entries, and preserves a summary of compacted entries for audit. Different from ORCH-007 (WAL replay) and ORCH-019 (checkpoint/restore) -- this is about WAL size management.

### ROAD-048: Deterministic replay of orchestrator decisions from WAL for debugging
**Priority**: p1 | **Complexity**: c3 | **Category**: Reliability Engineering
`bernstein replay --deterministic <run-id>` replays all orchestrator decisions from the WAL with mocked agent outcomes, producing the same task assignment and scheduling decisions. Enables debugging "why did the orchestrator assign task X to agent Y?" without re-running agents.

### ROAD-049: Graceful degradation when LLM provider has partial outage
**Priority**: p1 | **Complexity**: c2 | **Category**: Reliability Engineering
When a provider returns errors for some models but not others (e.g., Opus unavailable but Sonnet works), detect the partial outage pattern, automatically reroute affected tasks to available models, and resume with the original model when it recovers. Different from AGENT-004 (model fallback chain for all errors) -- this specifically handles partial provider outages with recovery detection.

### ROAD-050: Git garbage collection scheduling during low-activity windows
**Priority**: p1 | **Complexity**: c1 | **Category**: Reliability Engineering
Git repos with many worktrees and frequent operations accumulate loose objects. Running `git gc` during active orchestration causes lock contention. Schedule gc operations during detected low-activity windows (no active spawns, no pending merges) or between plan stages.

### ROAD-051: Cascading failure circuit breaker across dependent services
**Priority**: p2 | **Complexity**: c2 | **Category**: Reliability Engineering
When the task server is slow, agents queue up, exhausting memory. When git is slow, merges back up. Implement cross-service circuit breakers: if the task server latency exceeds 5s, pause spawning; if git operations exceed 30s, pause merging. Each service gets independent breaker state.

### ROAD-052: Automatic worktree orphan detection and cleanup on abnormal exit
**Priority**: p2 | **Complexity**: c1 | **Category**: Reliability Engineering
If the orchestrator crashes, worktrees from the crashed run persist and consume disk. On startup, detect worktrees that belong to no active run (by checking `.sdd/runtime/` session files) and offer to clean them up. `bernstein cleanup --force` removes all orphaned worktrees.

### ROAD-053: Task server connection pool with health-aware routing
**Priority**: p2 | **Complexity**: c2 | **Category**: Reliability Engineering
The orchestrator opens a new HTTP connection for each task server call. Under high concurrency, this exhausts ephemeral ports. Implement a connection pool with health-aware routing: prefer connections that responded quickly on the last request, retire connections that produced errors.

### ROAD-054: Structured chaos testing framework with configurable failure injection
**Priority**: p2 | **Complexity**: c3 | **Category**: Reliability Engineering
`bernstein chaos run --scenario network-flap --duration 30m` runs an orchestration while injecting failures: network drops, disk full simulation, process kills, clock skew. Records which failures caused data loss, task duplication, or incorrect results. Produces a reliability report card. Different from TEST-024 (chaos monkey for continuous testing) -- this is a structured, scenario-based chaos testing framework with reporting.

### ROAD-055: Heartbeat protocol v2 with bi-directional health negotiation
**Priority**: p2 | **Complexity**: c2 | **Category**: Reliability Engineering
Current heartbeats are agent-to-orchestrator only. Add orchestrator-to-agent health pings that can request: status update, progress report, context size, estimated remaining work. Agents that cannot respond to pings within 10s are marked degraded. Different from ORCH-005 (heartbeat timeout escalation) -- this adds bidirectional communication, not just timeout handling.

### ROAD-056: Idempotent merge operation with automatic conflict detection before attempt
**Priority**: p2 | **Complexity**: c2 | **Category**: Reliability Engineering
Before attempting a merge, do a dry-run merge check. If conflicts would occur, skip the merge, mark the task as "merge-blocked", and notify. Currently merges are attempted and conflicts are detected after partial merge state is created. Different from TASK-016 (diff preview before merge approval) -- this is about preventing the merge attempt entirely when conflicts are known.

### ROAD-057: Rolling restart capability for long-running orchestrations
**Priority**: p2 | **Complexity**: c2 | **Category**: Reliability Engineering
`bernstein restart --rolling` upgrades the orchestrator binary while agents continue running. The new orchestrator process inherits the WAL, reconnects to running agents via PID files, and resumes the tick loop. Zero downtime for orchestrator upgrades during multi-hour runs.

### ROAD-058: Task server request deduplication for idempotent processing
**Priority**: p2 | **Complexity**: c1 | **Category**: Reliability Engineering
Network retries can cause duplicate POST requests to the task server (e.g., two "complete task" requests). Add request-level deduplication using client-generated request IDs. The server returns the cached response for duplicate request IDs. Different from TASK-001 (idempotency tokens for state transitions) -- this is HTTP-level deduplication.

### ROAD-059: Predictive failure detection using agent behavior pattern analysis
**Priority**: p3 | **Complexity**: c3 | **Category**: Reliability Engineering
Analyze agent behavior patterns (tool call frequency, edit/undo ratio, repeated file reads) to predict failures before they happen. An agent that reads the same file 10 times in 2 minutes is likely stuck in a loop. An agent with a high undo ratio is likely producing low-quality work. Proactively intervene by injecting guidance or recycling the agent.

### ROAD-060: Self-healing orchestration that automatically retries with adjusted parameters
**Priority**: p3 | **Complexity**: c3 | **Category**: Reliability Engineering
When a task fails, analyze the failure mode and automatically adjust retry parameters: if context overflow, retry with a more aggressive compaction; if quality gate failure, retry with a stronger model; if merge conflict, retry with updated base branch. Different from the retry budget (TASK-010) which counts retries but does not adjust parameters.

### ROAD-061: Formal verification of orchestrator state machine transitions using TLA+ or Alloy
**Priority**: p3 | **Complexity**: c3 | **Category**: Reliability Engineering
Model the orchestrator state machine (task states, agent states, concurrent operations) in TLA+ or Alloy and verify: no deadlocks, no task duplication, no lost updates, eventual progress. Run the model checker in CI to catch state machine regressions. Different from formal_verification.py (which exists but is not a real model checker) -- this is actual formal methods.

---

## Dimension 4: Intelligence Layer (20 tickets)

How does Bernstein get smarter over time? These tickets introduce ML-powered routing, contextual bandit optimization, learning from failures, and predictive scheduling -- turning the orchestrator from a static dispatcher into an adaptive system.

### ROAD-062: Contextual bandit router that learns optimal model selection per task type
**Priority**: p0 | **Complexity**: c3 | **Category**: Intelligence Layer
Replace the static `role_model_policy` with a contextual bandit that observes (task_type, scope, complexity, language, repo_size) and selects the model that maximizes quality/cost ratio. Use Thompson Sampling with a Beta-Bernoulli reward model (success = quality gate pass). Warm-start from historical run data. Research shows 15-30% improvement over static routing (BaRP framework). The existing `bandit_router.py` file exists but implements only a stub.

### ROAD-063: Task difficulty estimator using code complexity metrics
**Priority**: p0 | **Complexity**: c2 | **Category**: Intelligence Layer
Before assigning a task, analyze the target files for: cyclomatic complexity, coupling, churn rate (git log frequency), test coverage, and number of dependents. Produce a difficulty score (1-10) that informs model selection, timeout, and retry budget. The existing `difficulty_estimator.py` uses only file count -- this adds code-level analysis.

### ROAD-064: Failure pattern classifier that categorizes recurring failure modes
**Priority**: p1 | **Complexity**: c2 | **Category**: Intelligence Layer
Maintain a failure pattern database: (error_type, file_pattern, role, model) -> (frequency, resolution). When a new failure occurs, match it against known patterns and suggest resolutions: "This looks like the 'import cycle' pattern seen 12 times. Resolution: split the task into two separate files." Different from error_classifier.py (which classifies individual exceptions) -- this is cross-run pattern learning.

### ROAD-065: Agent routing optimization using multi-armed bandit with cost constraints
**Priority**: p1 | **Complexity**: c3 | **Category**: Intelligence Layer
Extend the contextual bandit (ROAD-062) with a cost constraint: maximize quality subject to budget. When budget is tight, the bandit favors cheaper models; when budget is ample, it favors quality. Implements a constrained optimization using Lagrangian relaxation over the bandit's reward function.

### ROAD-066: Plan optimization engine that reorders tasks for minimum critical path time
**Priority**: p1 | **Complexity**: c2 | **Category**: Intelligence Layer
Given a task dependency graph, compute the critical path and reorder independent tasks to minimize wall-clock time. Use historical duration data per (role, scope, complexity) to estimate task durations. Display the optimized schedule as a Gantt chart. Different from TASK-006 (cycle detection) and TASK-014 (progress estimation) -- this is about optimization, not just validation.

### ROAD-067: Prompt effectiveness scoring based on outcome correlation
**Priority**: p1 | **Complexity**: c2 | **Category**: Intelligence Layer
Track which prompt variations (role templates, context files, instructions) correlate with task success. After N runs, identify: "Tasks using the 'explicit_constraints' prompt variant succeed 23% more often than the 'general' variant." Recommend prompt improvements. Different from prompt_versioning.py (which tracks versions) -- this correlates versions with outcomes.

### ROAD-068: Agent capability profiling that discovers per-model strengths from run data
**Priority**: p1 | **Complexity**: c2 | **Category**: Intelligence Layer
Build a capability profile per model from run data: "Opus excels at multi-file refactoring (94% success) but is average at test generation (71%). Sonnet is excellent at test generation (89%) and cheaper." Update the profile after each run. Feed into the bandit router. Different from AGENT-018 (performance profiling for spawn/token metrics) -- this is about task-type-specific quality.

### ROAD-069: Semantic deduplication of similar tasks across plan stages
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Detect semantically duplicate tasks: "Add input validation to UserController" and "Validate user input in UserController" are the same task with different wording. Use embedding similarity (local model) to detect duplicates at plan load time and warn. Different from duplicate_detector.py (which detects duplicate file content) -- this is task-level semantic deduplication.

### ROAD-070: Predictive cost model trained on historical token consumption patterns
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Train a regression model on (task_type, scope, complexity, model, file_count, code_complexity) -> token_consumption. Use for pre-run cost estimation with confidence intervals. Update the model incrementally after each run. Different from COST-010 (cost forecasting using averages) -- this uses a trained model, not simple averages.

### ROAD-071: Automatic prompt optimization using A/B testing framework
**Priority**: p2 | **Complexity**: c3 | **Category**: Intelligence Layer
For each role, maintain multiple prompt variants. Randomly assign variants to tasks and track quality gate pass rate per variant. After statistical significance is reached (using sequential testing to minimize sample size), promote the winning variant and introduce a new challenger. Continuous prompt improvement without human intervention.

### ROAD-072: Workload prediction model for proactive resource provisioning
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Predict upcoming resource needs based on plan analysis: "Stage 3 will require 8 concurrent agents in ~15 minutes. Current capacity: 5. Recommendation: increase max_agents or pre-warm 3 additional slots." Different from workload_prediction.py (which exists as a stub) -- this provides actionable forecasts.

### ROAD-073: Code quality regression detector across runs
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Track code quality metrics (lint errors, type errors, test failures, complexity) across runs. Alert when quality trends downward: "Over the last 5 runs, average lint errors per task increased from 0.3 to 1.7. Investigate recent prompt or model changes." Different from quality_score.py (which scores individual tasks) -- this is cross-run trend analysis.

### ROAD-074: Task decomposition quality scorer using historical completion data
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Score the quality of task decompositions by correlating decomposition patterns with outcomes. "Tasks decomposed into 3-5 subtasks succeed 85% of the time; tasks with 10+ subtasks succeed only 42%." Use this to recommend optimal decomposition granularity. Different from TASK-009 (decomposition validation) -- this uses statistical evidence, not rules.

### ROAD-075: Agent collaboration pattern mining from successful multi-agent runs
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Analyze successful multi-agent runs to discover effective collaboration patterns: "When backend and QA agents work on the same service, running QA immediately after backend (not in parallel) produces 30% fewer rework cycles." Feed patterns into the scheduler.

### ROAD-076: Adaptive timeout calculation based on task characteristics and model speed
**Priority**: p2 | **Complexity**: c1 | **Category**: Intelligence Layer
Instead of static timeouts, calculate per-task timeouts from: historical completion time for similar tasks, model's observed tokens-per-second, estimated task complexity. A simple refactoring task with Sonnet gets 10 minutes; a complex multi-file feature with Opus gets 45 minutes. Different from CLAUDE-014 (max_turns coordination) -- this is about overall timeout, not just turn count.

### ROAD-077: Embedding-based file relevance scoring for smarter context injection
**Priority**: p2 | **Complexity**: c2 | **Category**: Intelligence Layer
Before spawning an agent, compute relevance scores between the task description and all project files using local embeddings (e.g., `gte-small`). Inject the top-K most relevant files as context, regardless of what the task explicitly lists. Helps agents discover relevant code they would otherwise miss.

### ROAD-078: Reinforcement learning for orchestrator tick pipeline parameter tuning
**Priority**: p3 | **Complexity**: c3 | **Category**: Intelligence Layer
Use RL to optimize tick pipeline parameters: tick interval, spawn batch size, heartbeat timeout, merge queue priority. The reward signal is: tasks completed per hour, quality gate pass rate, and inverse cost. Train a lightweight policy network that adjusts parameters in real-time based on system state.

### ROAD-079: Transfer learning from successful runs to new projects
**Priority**: p3 | **Complexity**: c3 | **Category**: Intelligence Layer
When onboarding a new project, apply learned patterns from similar projects: "This Python FastAPI project is similar to 3 projects in our history. Using the learned model routing, prompt variants, and timeout settings from those projects." Requires a project embedding and similarity metric.

### ROAD-080: Natural language plan refinement via conversational LLM interface
**Priority**: p3 | **Complexity**: c2 | **Category**: Intelligence Layer
`bernstein plan chat` opens a conversation where users describe what they want and the LLM iteratively refines the plan: "Add a stage for database migration" -> LLM updates the plan YAML -> user reviews -> "Make the QA stage depend on both backend and migration" -> LLM updates. Interactive plan co-creation.

### ROAD-081: Causal inference engine for root cause analysis of orchestration failures
**Priority**: p3 | **Complexity**: c3 | **Category**: Intelligence Layer
Beyond correlation (ROAD-064), use causal inference methods (do-calculus, instrumental variables) to determine actual causes: "Task failures increased after switching to the new prompt template" vs "Task failures increased because the repo's complexity increased (coincidentally at the same time as the prompt change)." Distinguish confounders from causes.

---

## Dimension 5: Ecosystem & Partnerships (20 tickets)

How does Bernstein become the center of a thriving ecosystem rather than a standalone tool? IDE plugins, CI/CD integrations, marketplace for agent skills, and protocol interoperability.

### ROAD-082: VS Code extension with plan editing, run management, and live agent view
**Priority**: p0 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Build a VS Code extension that provides: plan YAML editing with autocomplete and validation, one-click run/stop/pause, live agent status panel, inline diff annotations (ROAD-025), and task detail inspector. This is the primary IDE for the majority of Bernstein's target users.

### ROAD-083: GitHub Actions reusable workflow for Bernstein-powered CI
**Priority**: p0 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Publish a reusable GitHub Actions workflow: `uses: bernstein-ai/action@v1` that installs Bernstein, runs a specified plan, and posts results (diff, cost, quality gate summary) as a PR comment. Include caching for agent warm pool and plan template resolution. This is the zero-friction CI integration path.

### ROAD-084: JetBrains plugin exposing Bernstein via ACP (Agent Client Protocol)
**Priority**: p1 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Build a JetBrains IDE plugin that integrates Bernstein via the Agent Client Protocol (ACP) announced by JetBrains in late 2025. Agents appear natively in the JetBrains agent panel alongside Junie and Kimi. Leverage the existing `acp.py` and `acp_ide_bridge.py` in core.

### ROAD-085: GitLab CI/CD integration template with merge request automation
**Priority**: p1 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Provide a GitLab CI template (`.gitlab-ci.yml` snippet) that runs Bernstein on merge request events, executes plans scoped to changed files, and adds review comments. Support GitLab's native merge request API for status updates.

### ROAD-086: Slack bot for orchestration management and notifications
**Priority**: p1 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
A Slack bot that provides: `/bernstein status` for run status, `/bernstein run <plan>` to trigger runs, real-time notifications for completions/failures/budget alerts, and threaded conversation for task details. Different from HOOK-013 (Slack notification template) -- this is a full interactive bot, not just one-way notifications.

### ROAD-087: MCP Server Card publishing for Bernstein's tools
**Priority**: p1 | **Complexity**: c1 | **Category**: Ecosystem & Partnerships
Following the 2026 MCP roadmap's emphasis on Server Cards for metadata discovery, publish MCP Server Cards for all Bernstein-provided tools (task management, status, cost, config). This allows MCP-aware clients to discover Bernstein's capabilities without connecting first.

### ROAD-088: Terraform/Pulumi provider for Bernstein infrastructure-as-code
**Priority**: p1 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
A Terraform provider that manages Bernstein resources declaratively: `resource "bernstein_plan" "migration"`, `resource "bernstein_webhook" "slack"`, `resource "bernstein_config" "production"`. Enables GitOps workflows for Bernstein configuration.

### ROAD-089: Plugin marketplace with versioned, signed, and reviewed community plugins
**Priority**: p2 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Build a plugin marketplace where the community publishes: quality gates, adapters, role templates, plan templates, hooks, and MCP server bundles. Each plugin has: a manifest, version, signature, review status, install count, and compatibility matrix. `bernstein plugin install <name>` from the marketplace. Different from MCP-014 (MCP server marketplace) -- this covers all plugin types.

### ROAD-090: Backstage plugin for developer portal integration
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Build a Backstage plugin that surfaces Bernstein runs, agent status, cost reports, and quality metrics within the organization's developer portal. Platform engineering teams increasingly standardize on Backstage; being absent from it is a deal-breaker.

### ROAD-091: Linear/Asana/Shortcut integration for project management sync
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Beyond GitHub Issues and Jira (ROAD-010), support Linear, Asana, and Shortcut as task sources. `bernstein sync linear --project my-project` pulls tasks, executes them, and updates status in the project management tool.

### ROAD-092: Datadog/New Relic APM integration for production observability
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Export Bernstein telemetry (spans, metrics, logs) to Datadog and New Relic using their native agents/SDKs. Different from the existing Prometheus/OTLP export -- this is native integration with proprietary APM platforms that most enterprises already use.

### ROAD-093: ArgoCD/Flux integration for GitOps deployment pipelines
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
When Bernstein completes a plan and merges code, trigger an ArgoCD or Flux sync to deploy the changes. Integrate with their webhook APIs and provide health checks that verify the deployment succeeded before marking the Bernstein run as fully complete.

### ROAD-094: Discord bot for open-source community support and orchestration demos
**Priority**: p2 | **Complexity**: c1 | **Category**: Ecosystem & Partnerships
A Discord bot for the Bernstein community: `/demo` runs a live orchestration on a sample repo and streams output to the channel, `/help <topic>` provides contextual help, `/status` shows community contribution stats. Community-building automation.

### ROAD-095: Neovim plugin with Lua-based Bernstein integration
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
A Neovim plugin providing: plan editing with treesitter-based highlighting, `:BernsteinRun` command, split-pane agent output, and inline diff annotations. Neovim users are a significant portion of CLI agent users.

### ROAD-096: Helm chart and Kubernetes operator for cluster deployment
**Priority**: p2 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Publish a Helm chart for deploying Bernstein in Kubernetes: task server as a Deployment, orchestrator as a StatefulSet, agents as Jobs. A Kubernetes operator watches CRDs (BernsteinPlan, BernsteinRun) and manages lifecycle. Different from ENT-013 (auto-scaling) -- this is the foundational K8s deployment mechanism.

### ROAD-097: OpenTelemetry Collector integration with pre-built dashboards
**Priority**: p2 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Ship a pre-configured OTel Collector config and Grafana dashboard JSONs that visualize: agent concurrency over time, task throughput, cost accumulation, error rates, and latency distributions. Different from WEB-009 (Grafana dashboard generator) -- this includes the full OTel pipeline configuration.

### ROAD-098: Agent skill marketplace where agents publish reusable tool compositions
**Priority**: p3 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
An extension of the plugin marketplace (ROAD-089) specifically for agent skills: "database-migration-validator", "api-contract-tester", "security-scanner-orchestrator". Each skill is a packaged MCP tool composition that agents can use. Community-contributed, version-managed, and rated.

### ROAD-099: Interoperability bridge with CrewAI crews and LangGraph workflows
**Priority**: p3 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Allow Bernstein to delegate tasks to CrewAI crews or LangGraph workflows as external executors, and vice versa. `adapter: crewai` in a task definition delegates to a CrewAI crew. This positions Bernstein as the orchestration layer above other frameworks rather than a competitor.

### ROAD-100: Language-specific SDK libraries (Python, TypeScript, Go, Rust) for embedding Bernstein
**Priority**: p3 | **Complexity**: c3 | **Category**: Ecosystem & Partnerships
Publish SDK libraries that allow developers to embed Bernstein orchestration in their own applications: `from bernstein import Orchestrator; orch = Orchestrator(); orch.run_plan("plan.yaml")`. Different from WEB-015 (API client SDK for the HTTP API) -- this embeds the orchestrator itself as a library.

### ROAD-101: Integration with cloud cost management platforms (CloudHealth, Spot.io, Kubecost)
**Priority**: p3 | **Complexity**: c2 | **Category**: Ecosystem & Partnerships
Export Bernstein cost data to cloud cost management platforms for unified cost visibility. Map agent costs to Kubernetes namespaces, cloud accounts, or cost centers. Enterprises need a single pane of glass for all AI spending.

---

## Dimension 6: Platform Architecture (20 tickets)

What architectural changes enable Bernstein to serve multi-tenant enterprises, federated teams, and global deployments? These tickets address the infrastructure layer beneath the orchestrator.

### ROAD-102: Event sourcing architecture for task state with full replay capability
**Priority**: p0 | **Complexity**: c3 | **Category**: Platform Architecture
Replace the current mutable task store with an event-sourced architecture: every state change is an immutable event (TaskCreated, TaskClaimed, TaskCompleted, etc.). Current state is derived by replaying events. This enables: time-travel debugging, audit-proof state history, and eventually-consistent replicas. Different from TASK-008 (status history) -- this is a fundamental architectural change from mutable state to event sourcing.

### ROAD-103: API gateway with request routing, throttling, and protocol translation
**Priority**: p0 | **Complexity**: c3 | **Category**: Platform Architecture
For multi-tenant and federated deployments, add an API gateway layer that: routes requests to the correct tenant's orchestrator, applies per-tenant rate limits, translates between API versions, and terminates TLS. Different from WEB-002 (rate limiting per endpoint) -- this is a dedicated gateway process, not middleware.

### ROAD-104: Pluggable storage backend abstraction with migration tooling
**Priority**: p1 | **Complexity**: c3 | **Category**: Platform Architecture
The existing store_postgres.py and store_redis.py provide alternative backends, but migrating between them (or from the default file store) requires manual data transfer. Add a storage migration tool: `bernstein storage migrate --from file --to postgres` that transfers all state, verifies integrity, and switches the active backend.

### ROAD-105: Asynchronous task queue with persistent backing (Redis Streams or NATS JetStream)
**Priority**: p1 | **Complexity**: c3 | **Category**: Platform Architecture
For high-throughput deployments (100+ tasks/hour), the HTTP-based task polling model becomes a bottleneck. Add an optional persistent message queue backend (Redis Streams or NATS JetStream) for task dispatch. Tasks are published to the queue and consumed by orchestrator workers. Different from the task server's polling API -- this is push-based.

### ROAD-106: Multi-region task routing with locality-aware scheduling
**Priority**: p1 | **Complexity**: c2 | **Category**: Platform Architecture
In multi-region deployments, route tasks to the orchestrator instance closest to the relevant code repository and LLM provider endpoint. `task.region_hint: us-east-1` ensures the task runs on the US East orchestrator, minimizing latency to both the git server and the API endpoint.

### ROAD-107: Orchestrator federation protocol for cross-instance task delegation
**Priority**: p1 | **Complexity**: c3 | **Category**: Platform Architecture
Allow multiple Bernstein instances to delegate tasks to each other. Instance A (Python specialist) delegates a Rust task to Instance B (Rust specialist). Uses the A2A protocol internally. Different from MCP-009 (A2A federation with external orchestrators) -- this is Bernstein-to-Bernstein federation.

### ROAD-108: Hot-swappable adapter loading without orchestrator restart
**Priority**: p1 | **Complexity**: c2 | **Category**: Platform Architecture
When a new adapter version is published or a new adapter is installed, load it without restarting the orchestrator. Use Python's importlib machinery to dynamically load adapter modules. Validate the new adapter with a health check before routing tasks to it.

### ROAD-109: Write-ahead log replication for high availability
**Priority**: p2 | **Complexity**: c3 | **Category**: Platform Architecture
Replicate the WAL to a standby orchestrator instance. If the primary fails, the standby replays uncommitted entries and takes over. Different from ENT-010 (disaster recovery with cross-region replication) -- this is single-region HA, not DR.

### ROAD-110: gRPC API alongside REST for high-performance internal communication
**Priority**: p2 | **Complexity**: c3 | **Category**: Platform Architecture
For cluster-internal communication (node-to-node, orchestrator-to-agent), add a gRPC API with protobuf serialization. Reduces latency and bandwidth compared to JSON-over-HTTP. REST remains the external API; gRPC is internal.

### ROAD-111: Capability-based addressing for agents (find by skill, not by name)
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
Instead of assigning tasks to specific adapters/models, specify required capabilities: `requires: [python, testing, refactoring]`. The router matches capabilities to available agents. Decouples task definitions from specific providers.

### ROAD-112: Content-addressable storage for agent outputs (deduplication across runs)
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
Store agent outputs (diffs, logs, artifacts) in a content-addressable store (keyed by SHA-256). When two runs produce identical outputs, store them once. Reduces storage for repetitive tasks (e.g., running the same quality gate on unchanged code).

### ROAD-113: Agent sandbox profiles with composable capability sets
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
Define composable sandbox profiles: `sandbox: web-backend` grants network access to localhost:5432 and localhost:6379 but nothing else. `sandbox: frontend` grants no network but allows npm install. Profiles are composed: `sandbox: [web-backend, ci-runner]` unions the capabilities. Different from the existing sandbox.py (which has binary on/off) -- this is composable profiles.

### ROAD-114: Plugin dependency resolution with version constraint satisfaction
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
Plugins (quality gates, adapters, hooks) may depend on other plugins or on specific Bernstein versions. Implement a dependency resolver (SAT-based, like pip's) that checks compatibility before installation and warns about conflicts. Different from plugin_reconciler.py (which reconciles loaded plugins) -- this is pre-installation dependency solving.

### ROAD-115: Ephemeral agent environments using lightweight VMs (Firecracker/gVisor)
**Priority**: p2 | **Complexity**: c3 | **Category**: Platform Architecture
For maximum isolation, run agents in lightweight VMs instead of containers. Firecracker VMs boot in <125ms and provide hardware-level isolation. Each agent gets a fresh VM with the repo mounted read-write. Different from sandbox.py (Docker/Podman containers) -- this is VM-level isolation for higher security requirements.

### ROAD-116: Task priority queue with fair scheduling across tenants
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
In multi-tenant deployments, a single tenant's burst of tasks can starve other tenants. Implement weighted fair queuing: each tenant gets a proportional share of agent slots based on their tier. Priority within a tenant follows the existing priority system.

### ROAD-117: Distributed tracing context propagation across all Bernstein components
**Priority**: p2 | **Complexity**: c2 | **Category**: Platform Architecture
Propagate W3C traceparent headers through: CLI -> API gateway -> task server -> orchestrator -> spawner -> agent -> quality gates -> merge. A single trace ID spans the entire lifecycle of a task from creation to merge. Different from ORCH-014 (spans for tick phases) and CLAUDE-020 (Claude trace correlation) -- this is end-to-end propagation.

### ROAD-118: Schema registry for task payloads with backward/forward compatibility validation
**Priority**: p2 | **Complexity**: c1 | **Category**: Platform Architecture
As the task payload schema evolves, ensure old agents can work with new task formats and vice versa. Maintain a schema registry with compatibility checks (like Avro's schema evolution rules). Block incompatible schema changes in CI.

### ROAD-119: Decentralized orchestration where agents self-organize without a central coordinator
**Priority**: p3 | **Complexity**: c3 | **Category**: Platform Architecture
Research mode: agents claim tasks from a shared bulletin board, negotiate dependencies via message passing, and self-coordinate without a central orchestrator. The orchestrator becomes optional -- useful for fault tolerance (no single point of failure) and for environments where a central server is not feasible.

### ROAD-120: WebAssembly-based plugin runtime for safe, portable plugin execution
**Priority**: p3 | **Complexity**: c3 | **Category**: Platform Architecture
Run plugins (quality gates, hooks, validators) in a WebAssembly sandbox. Plugins are compiled to WASM, get limited capabilities (file read, HTTP out), and cannot crash the host process. Enables running untrusted community plugins safely.

### ROAD-121: Append-only data lake for long-term analytics across all Bernstein instances
**Priority**: p3 | **Complexity**: c3 | **Category**: Platform Architecture
Export all events (tasks, agent sessions, costs, quality gate results) to a Parquet-based data lake (S3/GCS). Enable SQL analytics across months of orchestration history: "What percentage of security tasks failed in Q1 vs Q2?", "Which model has the best cost-efficiency trend?"

---

## Dimension 7: Security & Compliance (20 tickets)

What would a CISO want before approving Bernstein for production? These tickets go beyond the existing permission/security dimension (Dimension 4 in the 300-task plan) to address zero-trust architecture, compliance automation, pen testing, and supply chain security at the depth enterprises require.

### ROAD-122: Zero-trust agent authentication where every agent proves identity per request
**Priority**: p0 | **Complexity**: c3 | **Category**: Security & Compliance
Currently, agents inherit the orchestrator's authentication context. Implement per-agent identity: each agent gets a short-lived JWT signed by the orchestrator, scoped to its task and file permissions. The task server validates the JWT on every request. Compromising one agent does not grant access to another agent's scope.

### ROAD-123: Automated SBOM generation for every agent-produced artifact
**Priority**: p0 | **Complexity**: c2 | **Category**: Security & Compliance
When an agent adds a dependency (`pip install`, `npm install`, etc.), automatically generate a Software Bill of Materials (SBOM) in SPDX or CycloneDX format. Run vulnerability scanning (via `osv-scanner` or `grype`) against the SBOM before allowing merge. Enterprises increasingly require SBOM for all code changes.

### ROAD-124: Immutable audit trail with cryptographic attestation (Sigstore/Rekor)
**Priority**: p1 | **Complexity**: c3 | **Category**: Security & Compliance
Extend the HMAC-chained audit log with Sigstore-based attestation. Every task completion is signed with a keyless signature (Fulcio) and recorded in the transparency log (Rekor). Provides non-repudiable proof that a specific agent produced a specific diff at a specific time. Different from audit_integrity.py (HMAC chain) -- this is third-party cryptographic attestation.

### ROAD-125: HIPAA compliance mode with PHI detection and data handling controls
**Priority**: p1 | **Complexity**: c2 | **Category**: Security & Compliance
A `compliance: hipaa` configuration mode that: enables PHI detection in all agent inputs/outputs (using NER for names, dates, SSNs, MRNs), blocks agents from accessing files matching PHI patterns, enforces encryption at rest for all state files, and generates a BAA-ready compliance report. Different from pii_output_gate.py (which gates PII in output) -- this is comprehensive HIPAA compliance mode.

### ROAD-126: Agent behavior anomaly detection for compromised agent sessions
**Priority**: p1 | **Complexity**: c2 | **Category**: Security & Compliance
Monitor agent behavior for anomalies that suggest compromise: accessing files outside scope, running unexpected commands, generating unusually large outputs, or communicating with unexpected network endpoints. Uses the existing behavior_anomaly.py but adds real-time detection with automatic agent suspension. Different from SEC-003 (permission denial tracking) -- this detects anomalies in allowed behavior, not just denied actions.

### ROAD-127: Pen testing harness for Bernstein's attack surface
**Priority**: p1 | **Complexity**: c3 | **Category**: Security & Compliance
Build an automated pen testing suite that attacks: the API server (auth bypass, injection, SSRF), the agent spawn pipeline (prompt injection, command injection), the MCP gateway (tool abuse, resource exhaustion), and the merge pipeline (malicious diff injection). Run monthly in CI. Different from TEST-021 (security-focused tests) -- this is adversarial testing with attack simulation.

### ROAD-128: EU AI Act compliance module with risk classification and documentation
**Priority**: p1 | **Complexity**: c2 | **Category**: Security & Compliance
The EU AI Act's high-risk system requirements take effect August 2026. Build a compliance module that: classifies Bernstein deployments by risk level, generates required technical documentation (data governance, accuracy metrics, human oversight procedures), and produces the conformity assessment evidence package. Extends eu_ai_act.py which currently exists as a stub.

### ROAD-129: Runtime secrets vault integration with just-in-time credential injection
**Priority**: p1 | **Complexity**: c2 | **Category**: Security & Compliance
Instead of persisting API keys in config, integrate with HashiCorp Vault, AWS Secrets Manager, and 1Password for just-in-time credential injection. Credentials are fetched at spawn time, passed to the agent via environment variables, and revoked when the agent exits. Different from secrets.py (which supports vault/AWS/1Password for storage) -- this adds just-in-time injection and automatic revocation.

### ROAD-130: Code signing for agent-produced commits with verifiable provenance
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
Sign every commit produced by a Bernstein agent with a GPG or SSH key that identifies the specific agent, task, and run. `git log --show-signature` reveals provenance. Enterprises with signed-commit requirements can adopt Bernstein without weakening their commit policy.

### ROAD-131: Data loss prevention (DLP) scanning for agent outputs
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
Beyond secret detection (SEC-006), scan agent outputs for: source code from other projects (license violation), proprietary data patterns (customer IDs, internal URLs), and regulated data (credit card numbers, health records). Block merge when DLP violations are detected.

### ROAD-132: Agent sandboxing with seccomp-bpf syscall filtering
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
For Linux deployments, apply seccomp-bpf profiles to agent processes that restrict which system calls they can make. An agent that only needs to read/write files and make HTTP calls should not be able to: mount filesystems, load kernel modules, or create network sockets. Different from ROAD-115 (VM isolation) -- this is syscall-level filtering within the same OS.

### ROAD-133: Compliance-as-code policy library with 50+ pre-built rules
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
Publish a library of compliance policies in OPA/Rego format: SOC 2 controls, ISO 27001 controls, PCI DSS requirements, NIST 800-53 controls. `bernstein compliance enable soc2` activates the relevant policies. Different from ENT-004 (SOC 2 evidence collection) -- this is proactive policy enforcement, not retrospective evidence collection.

### ROAD-134: Graduated access control that restricts new agents and expands trust over time
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
New agents start with minimal permissions (read-only, no network, limited files). As they demonstrate correct behavior (3+ successful tasks with no security violations), their trust level increases and permissions expand. An agent's trust score persists across runs. Mimics how enterprises onboard new contractors.

### ROAD-135: Cross-tenant data isolation verification with automated testing
**Priority**: p2 | **Complexity**: c2 | **Category**: Security & Compliance
Automated tests that verify tenant isolation: create tasks in Tenant A, verify Tenant B cannot read/modify them, verify Tenant A's WAL does not leak into Tenant B's namespace, verify cost data is strictly partitioned. Run as part of CI for every release. Different from ENT-001 (implementing tenant isolation) -- this is verifying isolation works correctly.

### ROAD-136: Agent credential scope minimization (least-privilege API keys)
**Priority**: p2 | **Complexity**: c1 | **Category**: Security & Compliance
Instead of giving every agent the same API key with full access, generate scoped API keys per agent: a backend agent gets a key that only works for code generation, not for web search or file analysis MCP tools. Requires provider support (Anthropic API key scoping) or local proxy-based enforcement.

### ROAD-137: Security incident response automation with containment procedures
**Priority**: p2 | **Complexity**: c3 | **Category**: Security & Compliance
When a security event is detected (sandbox escape attempt, credential exfiltration, anomalous behavior), automatically execute a containment procedure: kill the agent, quarantine its worktree, snapshot the state for forensics, block the task from retry, and notify the security team. Different from SEC-022 (security event correlation) -- this is automated response, not just correlation.

### ROAD-138: FedRAMP compliance assessment toolkit for government deployments
**Priority**: p3 | **Complexity**: c3 | **Category**: Security & Compliance
Produce the documentation and evidence required for FedRAMP authorization: System Security Plan (SSP), Plan of Action & Milestones (POA&M), and continuous monitoring reports. Automate evidence collection from Bernstein's audit trail. This opens the US government market.

### ROAD-139: Post-quantum cryptography readiness for audit log signatures
**Priority**: p3 | **Complexity**: c2 | **Category**: Security & Compliance
Replace HMAC-SHA256 in the audit chain with a post-quantum signature scheme (CRYSTALS-Dilithium or SPHINCS+) to ensure audit trail integrity against quantum computing attacks. NIST PQC standards finalized in 2024; early adoption signals security leadership.

### ROAD-140: Automated vulnerability disclosure program with bug bounty integration
**Priority**: p3 | **Complexity**: c2 | **Category**: Security & Compliance
Set up a security.txt, SECURITY.md, and integration with a bug bounty platform (HackerOne, Bugcrowd). Define scope, rewards, and response SLAs. Provide a sandboxed Bernstein instance for security researchers to test against.

### ROAD-141: Privacy-preserving telemetry using secure multi-party computation
**Priority**: p3 | **Complexity**: c3 | **Category**: Security & Compliance
For enterprises that want to share aggregate performance data without revealing proprietary details, implement MPC-based telemetry aggregation. Multiple Bernstein instances contribute encrypted metrics; only the aggregate is revealed. No single party can reconstruct another's data.

---

## Dimension 8: Observability & Analytics (20 tickets)

How do you debug a 50-agent orchestration that produced wrong code? How do you understand which agents are efficient and which are wasting money? These tickets build the observability layer that makes Bernstein's behavior transparent.

### ROAD-142: Orchestration flamegraph showing time distribution across all agents and phases
**Priority**: p0 | **Complexity**: c3 | **Category**: Observability & Analytics
`bernstein flamegraph <run-id>` generates a flamegraph (SVG/HTML) showing where wall-clock time was spent: spawn latency (per agent), LLM API wait time, tool execution time, merge time, quality gate time. Each agent is a column; phases are stacked rows. This is the single most powerful debugging tool for "why did my run take so long?"

### ROAD-143: Cost intelligence dashboard with spend attribution down to individual LLM calls
**Priority**: p1 | **Complexity**: c2 | **Category**: Observability & Analytics
A web dashboard page that breaks down cost at every level: run -> stage -> task -> agent session -> individual LLM API call. Each call shows: input tokens, output tokens, cache status, model, and cost. Identifies the exact API call that was most expensive. Different from COST-008 (per-agent cost attribution) -- this goes to individual API call granularity.

### ROAD-144: Agent decision tree visualization showing why each tool call was made
**Priority**: p1 | **Complexity**: c3 | **Category**: Observability & Analytics
For a selected agent session, render a decision tree: "Agent read file A -> decided to modify function B -> ran tests -> test failed -> read error output -> modified function B again -> tests passed." Interactive, zoomable, with the raw LLM output at each decision node. This answers "what was the agent thinking?"

### ROAD-145: Real-time anomaly alerting for orchestration metrics
**Priority**: p1 | **Complexity**: c2 | **Category**: Observability & Analytics
Apply statistical anomaly detection (Z-score, IQR, or isolation forest) to real-time metrics: token consumption rate, error frequency, task completion rate, cost velocity. Alert immediately (not after the run) when metrics are anomalous. Different from ORCH-022 (tick duration anomaly) and cost_anomaly.py (cost-specific) -- this is cross-metric anomaly detection.

### ROAD-146: Multi-run comparison dashboard for A/B testing orchestration strategies
**Priority**: p1 | **Complexity**: c2 | **Category**: Observability & Analytics
Compare two runs side-by-side: same plan with different configurations (model, prompt, concurrency). Show: completion time delta, cost delta, quality delta, per-task comparison. Statistical significance test for the differences. Different from the A/B test system (ab_test.py) -- this is visual comparison, not just data collection.

### ROAD-147: Token waste analyzer that identifies unnecessary context and redundant tool calls
**Priority**: p1 | **Complexity**: c2 | **Category**: Observability & Analytics
Analyze agent conversations post-run to identify: files read but never used, context injected but never referenced, tool calls that were repeated unnecessarily, and conversation segments where the agent was "going in circles." Estimate wasted tokens and cost. Different from token_waste_report.py (which exists as a stub) -- this is a full analysis engine.

### ROAD-148: Custom metric definition language for domain-specific KPIs
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
Allow users to define custom metrics in `bernstein.yaml`: `metrics: code_per_dollar: { formula: "lines_changed / total_cost", unit: "lines/$" }`. Custom metrics appear in dashboards, reports, and alerts alongside built-in metrics.

### ROAD-149: Distributed log aggregation with structured search across all agent sessions
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
Aggregate logs from all agents, the orchestrator, task server, and MCP servers into a single searchable index. `bernstein logs search "TypeError" --time-range "last 1h" --agent-role backend` finds all TypeErrors from backend agents in the last hour. Different from agent_log_aggregator.py (which collects logs) -- this adds structured search with time-range and attribute filtering.

### ROAD-150: SLA/SLO dashboard with burn-down rate visualization
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
Visualize SLO burn rates: "Task completion SLO is at 94.2% over the last 30 days, burning at 0.3%/day. At this rate, the SLO will breach in 6 days." Uses the error budget concept from SRE. Different from ENT-005 (SLA monitoring with breach alerting) -- this is visual burn-down rate analysis.

### ROAD-151: Quality trend dashboard showing code quality metrics over time
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
Track quality metrics across runs: lint errors per task, type errors per task, test pass rate, code review score. Show trends over weeks/months. Detect quality degradation early. Different from ROAD-073 (intelligence-layer regression detection) -- this is pure visualization, not ML-based detection.

### ROAD-152: Agent utilization heatmap showing active/idle/waiting time per agent per minute
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
A heatmap visualization (time on X-axis, agents on Y-axis) with color coding: green=active (executing tools), yellow=waiting (LLM API call pending), red=idle (no work assigned), blue=merging. Identifies parallelism gaps. Different from AGENT-021 (utilization dashboard with active vs idle ratio) -- this is a time-series heatmap, not aggregate ratios.

### ROAD-153: Automated post-mortem report generation for failed runs
**Priority**: p2 | **Complexity**: c2 | **Category**: Observability & Analytics
When a run fails or produces poor results, `bernstein postmortem <run-id>` generates a structured report: timeline of events, root cause analysis (using failure pattern database from ROAD-064), contributing factors, agent decision traces for failed tasks, and recommended actions. Exportable as HTML/PDF.

### ROAD-154: Real-time cost-per-line-of-code efficiency metric
**Priority**: p2 | **Complexity**: c1 | **Category**: Observability & Analytics
Calculate and display cost efficiency as the run progresses: "Current efficiency: $0.003/line. Run average: $0.005/line. Historical average: $0.004/line." Helps users understand if this run is unusually expensive.

### ROAD-155: Provider API latency tracker with historical percentile charts
**Priority**: p2 | **Complexity**: c1 | **Category**: Observability & Analytics
Track LLM API response latency per provider per model: p50, p95, p99 over time. Detect provider degradation before it causes agent timeouts. Display in dashboard and alert when latency exceeds historical p99 by 2x.

### ROAD-156: Agent token consumption breakdown (system prompt vs user vs output)
**Priority**: p2 | **Complexity**: c1 | **Category**: Observability & Analytics
For each agent session, show token breakdown: system prompt tokens (how much is Bernstein's overhead?), context file tokens, task description tokens, assistant output tokens, tool result tokens. Identifies optimization opportunities (e.g., "60% of tokens were spent on context files the agent never used").

### ROAD-157: Predictive alerting that forecasts issues before they impact the run
**Priority**: p3 | **Complexity**: c3 | **Category**: Observability & Analytics
Use time-series forecasting (Prophet, ARIMA) on metrics streams to predict: "At current cost velocity, budget will be exhausted in 12 minutes" or "Task completion rate is declining; estimated run duration will exceed the 4-hour window." Alert before the problem occurs, not after.

### ROAD-158: Natural language analytics queries over orchestration data
**Priority**: p3 | **Complexity**: c2 | **Category**: Observability & Analytics
`bernstein analytics "What was the most expensive task type last month?"` translates natural language to SQL/analytics queries over the data lake (ROAD-121) and returns a formatted answer. Removes the need to learn a query language for ad-hoc analysis.

### ROAD-159: Comparative analytics across organizations (anonymized benchmarks)
**Priority**: p3 | **Complexity**: c3 | **Category**: Observability & Analytics
With opt-in, contribute anonymized performance metrics to a shared benchmark: "Your task completion rate (91%) is in the 75th percentile across all Bernstein users. Top performers average 97%." Drives competitive improvement without revealing proprietary details. Uses privacy-preserving aggregation from ROAD-141.

### ROAD-160: Digital twin simulation of orchestration scenarios
**Priority**: p3 | **Complexity**: c3 | **Category**: Observability & Analytics
`bernstein simulate --plan my-plan.yaml --agents 20 --failure-rate 0.1` simulates the orchestration without running real agents. Uses historical data to model: agent completion times, failure probabilities, merge conflict rates. Outputs: predicted wall-clock time, cost, bottleneck analysis. Different from ORCH-021 (canary mode) which runs against real task streams -- this is pure simulation.

### ROAD-161: Correlation analysis between code complexity and agent performance
**Priority**: p3 | **Complexity**: c2 | **Category**: Observability & Analytics
Correlate code metrics (cyclomatic complexity, fan-in/fan-out, churn rate) with agent outcomes (success rate, time-to-completion, cost). Generate actionable insights: "Files with complexity >15 have a 3x higher agent failure rate. Consider pre-simplifying complex modules before assigning agent tasks."

---

## Dimension 9: Agent Quality (20 tickets)

How do you ensure that agent-produced code is correct, secure, and maintainable? These tickets address output verification, code review quality, merge safety, and the feedback loops that improve agent quality over time.

### ROAD-162: Multi-agent consensus verification where N agents independently verify each other's work
**Priority**: p0 | **Complexity**: c3 | **Category**: Agent Quality
For critical tasks (security fixes, data migrations, API changes), spawn N verifier agents (default 2) that independently review the producing agent's output. Merge only if >50% of verifiers approve. Uses different models for verifiers than the producer to avoid correlated errors. This is the nuclear option for correctness.

### ROAD-163: Semantic diff analysis that verifies behavior preservation after refactoring
**Priority**: p0 | **Complexity**: c3 | **Category**: Agent Quality
Beyond syntactic diff review, verify that refactoring tasks preserve behavior: extract function signatures before and after, verify all call sites are updated, check that return types are compatible, and run targeted tests for changed functions. Catches the class of bugs where "the code looks right but the behavior changed."

### ROAD-164: Automated integration test generation for agent-produced code changes
**Priority**: p1 | **Complexity**: c2 | **Category**: Agent Quality
After an agent completes a task, automatically generate integration tests that verify the change works in context (not just in isolation). Use the existing test framework to: create a test that exercises the changed code path, run it, and fail the quality gate if the test does not pass.

### ROAD-165: Code review scoring rubric with per-dimension feedback (style, correctness, performance, security)
**Priority**: p1 | **Complexity**: c2 | **Category**: Agent Quality
Score every agent-produced diff on 5 dimensions: style compliance (0-10), correctness (0-10), performance impact (0-10), security (0-10), maintainability (0-10). Aggregate into a composite score. Tasks below threshold trigger automatic rework or human review. Different from quality_score.py (which gives a single score) -- this is multi-dimensional.

### ROAD-166: Dependency impact analysis that prevents breaking changes
**Priority**: p1 | **Complexity**: c2 | **Category**: Agent Quality
Before merging, analyze which other modules import/depend on the changed code. If the agent modified a function signature, check all call sites (even outside the agent's scope) for compatibility. Block merge if breaking changes are detected. Different from dep_validator.py (which validates dependencies in plans) -- this is runtime code dependency analysis.

### ROAD-167: Agent output fingerprinting for detecting copy-paste from training data
**Priority**: p1 | **Complexity**: c2 | **Category**: Agent Quality
Detect when agent output is likely copied verbatim from training data (license risk). Compare output against a corpus of known open-source code using MinHash/LSH similarity. Flag matches above a configurable threshold for human review. Addresses the growing concern about LLM-generated code and licensing.

### ROAD-168: Incremental type-checking that validates only changed files plus dependents
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
Running full type-checking (Pyright/mypy) on a large codebase after each agent completion is slow. Implement incremental type-checking that: identifies changed files, traces import dependencies, and type-checks only the affected subgraph. Reduces quality gate time from minutes to seconds.

### ROAD-169: Automatic code formatting enforcement before merge (not just linting)
**Priority**: p2 | **Complexity**: c1 | **Category**: Agent Quality
Automatically run formatters (black/ruff format for Python, prettier for JS/TS, rustfmt for Rust) on agent output before merge. If the agent produced correctly-functioning but poorly-formatted code, fix it automatically rather than failing the quality gate. Different from the existing lint gate (which reports errors) -- this auto-fixes formatting.

### ROAD-170: Test mutation verification that confirms agent-written tests actually catch bugs
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
When an agent writes tests, run mutation testing on the code under test. If the mutations survive (tests do not catch them), the tests are weak and the quality gate should flag it. Different from TEST-015 (mutation testing for Bernstein's own tests) -- this is for agent-produced tests.

### ROAD-171: Architecture conformance checking against declared module boundaries
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
Define module boundaries in a config file: "module auth can import from module db but not from module api." Verify that agent-produced code respects these boundaries. Prevents agents from creating unwanted coupling between modules.

### ROAD-172: Regression test suite auto-expansion based on agent-produced changes
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
When an agent modifies code, automatically identify tests that should cover the change (using coverage data or call graph analysis). If no existing tests cover the change, add the file to a "needs test coverage" list and optionally spawn a test-writing agent.

### ROAD-173: Comment quality analysis for agent-produced documentation
**Priority**: p2 | **Complexity**: c1 | **Category**: Agent Quality
Verify that agent-produced comments and docstrings are: accurate (match the code), not redundant (do not restate the obvious), complete (cover parameters, returns, exceptions), and use the correct style (Google/NumPy/reST as configured).

### ROAD-174: Dead code detection after agent modifications
**Priority**: p2 | **Complexity**: c1 | **Category**: Agent Quality
After an agent modifies code, check if the changes created dead code: unreachable branches, unused imports, unused variables, functions that lost all callers. Agents sometimes add new implementations without removing old ones. Auto-fix or flag for cleanup.

### ROAD-175: Cross-agent consistency checker for multi-agent feature implementations
**Priority**: p2 | **Complexity**: c3 | **Category**: Agent Quality
When multiple agents implement different parts of the same feature (e.g., backend agent creates the API, frontend agent creates the UI), verify consistency: API endpoint names match, request/response schemas are compatible, error codes are handled. This catches the integration bugs that are invisible to each individual agent.

### ROAD-176: Performance benchmark gate that rejects changes causing >10% regression
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
Run a configurable performance benchmark before and after agent changes. If wall-clock time, memory usage, or throughput regresses by more than a configurable threshold (default 10%), block the merge. Essential for performance-sensitive codebases.

### ROAD-177: LLM-as-judge evaluation using a separate model to score agent output quality
**Priority**: p2 | **Complexity**: c2 | **Category**: Agent Quality
Use a separate LLM (different from the producing agent's model) to evaluate the quality of agent output against the task description. "Did the agent accomplish the task? Is the code correct? Are edge cases handled? Is the code maintainable?" Score each dimension. Uses the Haiku-as-judge pattern for cost efficiency.

### ROAD-178: Proof-carrying code verification for critical agent-produced changes
**Priority**: p3 | **Complexity**: c3 | **Category**: Agent Quality
For safety-critical code (financial calculations, auth logic, crypto), require agents to produce formal properties alongside code: pre/post conditions, loop invariants, or Hoare logic assertions. Verify the properties automatically using Z3 or Dafny. This is the gold standard for correctness.

### ROAD-179: Automated changelog generation from agent-produced diffs
**Priority**: p3 | **Complexity**: c1 | **Category**: Agent Quality
After a run, generate a human-readable changelog from all merged diffs: group changes by component, summarize each change in plain English, flag breaking changes, and link to the originating task. Different from changelog.py (which tracks Bernstein's own changelog) -- this generates changelogs for the target project.

### ROAD-180: Long-horizon code quality tracking with "code health" score per file
**Priority**: p3 | **Complexity**: c2 | **Category**: Agent Quality
Maintain a per-file "code health" score that tracks: complexity over time, bug density (from agent failures touching this file), test coverage, churn rate, and coupling. Agents should improve code health, not degrade it. Flag tasks that worsen code health for review.

### ROAD-181: Reproducibility verification that confirms identical inputs produce identical outputs
**Priority**: p3 | **Complexity**: c3 | **Category**: Agent Quality
Run the same task twice with the same model and temperature=0. Compare outputs. While LLMs are inherently non-deterministic, high divergence on the same task indicates prompt instability. Track reproducibility scores per task type and alert when they degrade.

---

## Dimension 10: Future-proofing (20 tickets)

What will the landscape look like when models are 10x cheaper and 10x smarter? When context windows are 10M tokens? When agents can run for days? These tickets prepare Bernstein for the 2-4 year horizon.

### ROAD-182: Model-agnostic routing layer that treats all providers as interchangeable backends
**Priority**: p0 | **Complexity**: c3 | **Category**: Future-proofing
Abstract all model-specific logic behind a unified routing interface: `route(task) -> (provider, model, config)`. The router considers: task requirements, model capabilities, cost, latency, availability, and learned preferences. Adding a new provider should require only implementing a thin adapter, not touching routing logic. Different from the existing router.py (which routes based on policy) -- this is a complete abstraction layer that decouples task scheduling from provider specifics.

### ROAD-183: Infinite context strategy for tasks that exceed any single model's context window
**Priority**: p1 | **Complexity**: c3 | **Category**: Future-proofing
When a task requires more context than any available model supports, automatically: segment the context into chunks, create subtasks that each process a chunk, synthesize results across chunks, and verify consistency. This enables Bernstein to handle tasks on codebases larger than any model's context window.

### ROAD-184: Multi-modal agent support (code + images + diagrams + architecture docs)
**Priority**: p1 | **Complexity**: c2 | **Category**: Future-proofing
Extend the agent interface to handle multi-modal inputs: architecture diagrams (image -> code), UI mockups (image -> implementation), data flow diagrams (image -> pipeline code). As models become natively multi-modal, orchestrators that only handle text will be left behind.

### ROAD-185: Agentic workflow compiler that compiles plans to optimized execution graphs
**Priority**: p1 | **Complexity**: c3 | **Category**: Future-proofing
Instead of interpreting plan YAML at runtime, compile it to an optimized execution graph (similar to how TensorFlow compiles computation graphs). The compiler can: fuse sequential tasks that share context, parallelize independent branches, pre-compute resource requirements, and optimize for cache reuse. This becomes important when plans have 100+ tasks.

### ROAD-186: Agent memory persistence across runs (long-term project knowledge)
**Priority**: p1 | **Complexity**: c2 | **Category**: Future-proofing
Agents currently start fresh every run. Allow agents to accumulate project knowledge across runs: "Last time I worked on auth.py, the team preferred JWT over session tokens. The CI pipeline requires Python 3.12+. The database ORM is SQLAlchemy 2.0." Store in a per-project vector database (local, using ChromaDB or similar). Different from lessons.py (which stores text lessons) -- this is embedding-based semantic memory.

### ROAD-187: Protocol version negotiation for forward/backward compatibility
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
As MCP, A2A, and ACP protocols evolve, Bernstein must negotiate protocol versions with peers. Implement version negotiation: advertise supported versions, negotiate the highest common version, and gracefully degrade when the peer only supports an older version. Different from MCP-012 (version compatibility checking) -- this is active negotiation, not just checking.

### ROAD-188: Agent-to-agent direct communication channel (bypass orchestrator for coordination)
**Priority**: p2 | **Complexity**: c3 | **Category**: Future-proofing
For tightly-coupled tasks (e.g., backend and frontend agents implementing the same API), allow direct communication: "What response schema did you use for /api/users?" This reduces orchestrator bottleneck and enables richer collaboration. Uses the bulletin board (bulletin_board.py) as the communication medium but adds structured query/response semantics.

### ROAD-189: Streaming task results for long-running agents (incremental merge)
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
Instead of waiting for an agent to complete before merging, allow incremental merges as the agent produces results. A test-writing agent that has written 5 of 10 test files can merge the first 5 while still working on the remaining 5. Reduces wall-clock time for long tasks.

### ROAD-190: Cost-per-quality Pareto frontier optimization across model configurations
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
For each task type, compute the Pareto frontier of (cost, quality) across all available model configurations. Present to users: "You can achieve 95% quality at $0.08/task with Sonnet, or 99% quality at $0.52/task with Opus. The Pareto-optimal choice depends on your quality requirement." Helps users make informed cost-quality tradeoffs.

### ROAD-191: Plugin hot-reloading with version rollback on failure
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
When a plugin update causes failures, automatically roll back to the previous version. Track plugin version history, detect quality gate pass rate degradation after an update, and trigger rollback. Different from ROAD-108 (hot-swappable adapter loading) -- this covers all plugin types with automatic rollback.

### ROAD-192: Speculative execution for branching task graphs
**Priority**: p2 | **Complexity**: c3 | **Category**: Future-proofing
When a task graph has conditional branches (if feature flag is set, do A; otherwise do B), speculatively execute both branches in parallel. When the condition is resolved, discard the unused branch. Trades compute cost for latency reduction on critical paths.

### ROAD-193: Sub-task streaming where parent agents observe child progress in real-time
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
When a manager agent decomposes a task and spawns subtask agents, stream subtask progress back to the manager. The manager can intervene ("subtask 3 is going off track, redirect it") without waiting for completion. Enables tighter coordination loops.

### ROAD-194: Energy-aware scheduling that considers carbon intensity of compute regions
**Priority**: p2 | **Complexity**: c2 | **Category**: Future-proofing
Route tasks to regions with lower carbon intensity when timing allows. Use real-time carbon intensity APIs (WattTime, Electricity Maps) to choose between US-East (coal-heavy) and US-West (hydro-heavy). Non-urgent tasks wait for low-carbon windows. Increasingly important as AI energy consumption faces regulatory scrutiny.

### ROAD-195: Autonomous orchestrator mode that generates its own plans from repository analysis
**Priority**: p3 | **Complexity**: c3 | **Category**: Future-proofing
`bernstein auto` analyzes the repository (open issues, TODO comments, failing tests, outdated dependencies, code quality hotspots) and generates a prioritized plan without human input. Executes the plan and produces a summary of what was improved. The fully autonomous software engineering team.

### ROAD-196: Agent specialization through fine-tuning on project-specific coding patterns
**Priority**: p3 | **Complexity**: c3 | **Category**: Future-proofing
Collect successful agent outputs per project and use them to fine-tune a project-specific model adapter. The fine-tuned model knows the project's patterns, naming conventions, and architecture without requiring extensive context. Reduces token consumption and improves quality. Requires API support for fine-tuning (Anthropic, OpenAI).

### ROAD-197: Quantum-resistant state encryption for long-term audit trail security
**Priority**: p3 | **Complexity**: c2 | **Category**: Future-proofing
Encrypt state files (`.sdd/`) using hybrid encryption: classical AES-256 + post-quantum KEM (ML-KEM/CRYSTALS-Kyber). Ensures that state data encrypted today remains secure even when quantum computers can break classical encryption. Companion to ROAD-139 (post-quantum audit signatures).

### ROAD-198: Agent swarm mode for embarrassingly parallel tasks (1000+ simultaneous agents)
**Priority**: p3 | **Complexity**: c3 | **Category**: Future-proofing
For tasks like "add type hints to every file in a 500-file project," spawn one agent per file (500 agents simultaneously). Requires: cloud-based agent execution (not local), distributed merge coordination, and cost circuit breakers. When models are 10x cheaper, this becomes economically viable.

### ROAD-199: Self-improving orchestrator that rewrites its own scheduling algorithms
**Priority**: p3 | **Complexity**: c3 | **Category**: Future-proofing
Meta-level: the orchestrator uses its own agent orchestration to improve its own code. After each run, analyze orchestrator performance metrics, generate improvement tasks, and execute them against the Bernstein codebase itself. Convergence guards prevent infinite self-modification loops.

### ROAD-200: Cross-language orchestration where agents work across polyglot monorepos seamlessly
**Priority**: p3 | **Complexity**: c3 | **Category**: Future-proofing
A single plan that coordinates: Python backend agents, TypeScript frontend agents, Rust infrastructure agents, and Go service agents. Each agent uses language-appropriate tools and quality gates, but the orchestrator manages cross-language dependencies (e.g., API schema shared between Python backend and TypeScript frontend). Different from the existing multi-adapter support (which just picks different CLI tools) -- this is cross-language dependency awareness.

---

## Summary Statistics

| Priority | Count | Description |
|----------|-------|-------------|
| p0 | 20 | Existential -- could lose deals if missing |
| p1 | 50 | Important -- significant competitive advantage |
| p2 | 80 | Nice-to-have -- polish and depth |
| p3 | 50 | Future -- 2-4 year horizon, research/exploration |
| **Total** | **200** | |

| Complexity | Count | Description |
|------------|-------|-------------|
| c1 | ~55 | Small -- 1 agent session, few files |
| c2 | ~85 | Medium -- multiple files, needs design |
| c3 | ~60 | Large -- cross-cutting, needs architecture |

## Implementation Sequencing

**Phase A (Months 1-3): Market Entry Hardening**
Focus: p0 tickets across all dimensions. The one-command quickstart (ROAD-001), competitive benchmarks (ROAD-002), VS Code extension (ROAD-082), GitHub Actions integration (ROAD-083), and zero-trust agent auth (ROAD-122). 20 tickets.

**Phase B (Months 2-6): Intelligence & Developer Experience**
Focus: p1 tickets, especially: contextual bandit router (ROAD-062), agent replay debugger (ROAD-022), JetBrains ACP plugin (ROAD-084), orchestration flamegraph (ROAD-142), multi-agent consensus verification (ROAD-162), and SBOM generation (ROAD-123). 50 tickets.

**Phase C (Months 4-12): Platform Depth & Ecosystem**
Focus: p2 tickets. Marketplace (ROAD-089), chaos testing framework (ROAD-054), compliance-as-code library (ROAD-133), cross-agent consistency checking (ROAD-175), and the full observability stack. 80 tickets.

**Phase D (Months 9-24+): Future Vision**
Focus: p3 tickets. Autonomous orchestrator (ROAD-195), agent swarm mode (ROAD-198), self-improving orchestrator (ROAD-199), and the research-oriented biological colony (ROAD-201). 50 tickets.

## Relationship to Existing 300-Task Plan

This 200-ticket roadmap is ADDITIVE to the existing 300-task plan. The 300-task plan focuses on **internal quality** (hardening, correctness, reliability of existing code). This roadmap focuses on **external value** (product-market fit, ecosystem, intelligence, future-proofing).

Execute the 300-task plan's P0 items first (foundation), then interleave this roadmap's P0 items (market entry). The two plans share no ticket IDs and have no duplicate scope.

---

*Generated 2026-04-05 by VP Engineering analysis. Research sources include Deloitte, Gartner, IBM, and Anthropic/MCP Foundation publications.*
