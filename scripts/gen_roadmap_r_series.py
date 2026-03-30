#!/usr/bin/env python3
"""Generate 100 R-series roadmap tickets based on research signals.

Organized by impact on adoption/revenue/recognition:
- R01-R20: Developer Adoption (make devs CHOOSE Bernstein)
- R21-R35: Cost & ROI (make it UNDENIABLY cheaper)
- R36-R50: Quality & Trust (make output RELIABLY good)
- R51-R65: Enterprise Ready (unlock PAID customers)
- R66-R80: Ecosystem & Moat (create NETWORK EFFECTS)
- R81-R90: Thought Leadership (make Alex FAMOUS)
- R91-R100: Moonshots (differentiate from EVERYONE)
"""

from pathlib import Path

BACKLOG = Path(".sdd/backlog/open")
BACKLOG.mkdir(parents=True, exist_ok=True)

TICKETS: list[tuple[str, str, str, int, str, str, str]] = [
    # (id, slug, title, priority, scope, role, body)

    # ── R01-R20: Developer Adoption ──
    ("R01", "zero-config-first-run", "Zero-Config First Run Experience", 1, "medium", "backend",
     """First `bernstein -g "goal"` should work without ANY setup — no YAML, no init, no config.
Auto-detect project type, installed agents, and optimal routing. Create bernstein.yaml only if user wants to customize.
This is the #1 onboarding friction point (RedMonk Signal #5: 'only senior+ engineers use parallel agents successfully').
Acceptance: `cd any-project && bernstein -g "add tests"` works on first try with zero prior setup."""),

    ("R02", "60-second-demo-mode", "60-Second Demo Mode", 1, "small", "backend",
     """``bernstein demo`` creates a sample Flask/FastAPI app, runs 3 agents on it, shows results in 60 seconds.
No API keys needed (uses mock adapter). Produces real git commits, real test results, real cost report.
This is the YouTube/conference demo moment. Must be visually impressive and shareable.
Acceptance: works offline, <60s, produces 3+ commits, shows TUI dashboard."""),

    ("R03", "persistent-memory-v1", "Persistent Cross-Session Memory", 1, "large", "backend",
     """SQLite-backed memory: agent decisions, project conventions, learnings persist across sessions.
#1 most-requested feature (7 independent sources: Reddit, HN, Twitter, GitHub, ThoughtWorks Radar).
Types: conventions (code style), decisions (arch choices), learnings (what worked/failed).
Agents receive relevant memories in prompt. Memory decays over time. `bernstein memory list/add/remove`.
Privacy: memories never leave the machine. Acceptance: agent A learns 'use pytest fixtures', agent B in next session knows it."""),

    ("R04", "worktree-env-setup-hooks", "Worktree Environment Setup Hooks", 1, "medium", "backend",
     """Auto-setup worktree environments: symlink node_modules, copy .env files, handle port conflicts.
From claude-squad#260: 'worktree is missing everything not tracked by git — node_modules gone, .env gone, agent fails'.
Config: `worktree_hooks: {symlink: [node_modules, .venv], copy: [.env, .envrc], ports: auto}`.
Acceptance: agent in worktree can `npm test` / `pytest` without manual setup."""),

    ("R05", "context-handoff-protocol", "Structured Context Handoff Between Agents", 1, "medium", "backend",
     """When delegating subtasks, auto-generate focused context briefs. Strip unnecessary context to reduce tokens.
HN Signal #17: 'subagents suffer from Claude Contexting — handoff is the kiss of death'.
Generate: relevant files list, key decisions made, constraints, what NOT to touch.
Acceptance: subtask agent gets <5K tokens of context instead of 50K+ full codebase dump."""),

    ("R06", "merge-queue-fifo", "FIFO Merge Queue with Conflict Detection", 1, "medium", "backend",
     """Merge agent branches sequentially, detect conflicts BEFORE merge attempt.
From overstory/claude-squad: merge chaos is the #1 operational pain in multi-agent.
Queue: oldest task first. Pre-check: `git merge-tree` to detect conflicts without merging.
On conflict: pause queue, notify user, offer resolution options.
Acceptance: 5 agents complete → branches merge sequentially → no conflicts reach main."""),

    ("R07", "file-level-locking", "File-Level Locking for Agents", 1, "medium", "backend",
     """When agent claims a task, lock target files. Other agents see lock and work on something else.
From Forge/Stoneforge: 'file locking makes conflicts structurally impossible'.
Lock granularity: file-level (not dir-level). Lock stored in `.sdd/runtime/locks.json`.
Agents check locks before editing. If locked file needed: wait or pick different task.
Acceptance: two agents never modify the same file simultaneously."""),

    ("R08", "intent-verification-gate", "Intent Verification Quality Gate", 1, "medium", "qa",
     """Compare agent output against original task spec before merge. Does it match INTENT, not just 'work'?
HN Signal #15 (396 points): 'validation is the bottleneck — need to match intent, not just be functional'.
Use cheap model to verify: 'Task asked for X. Agent produced Y. Does Y satisfy X? [yes/no/partially]'.
Acceptance: tasks that diverge from spec are caught before merge."""),

    ("R09", "cross-model-verification", "Cross-Model Verification Pipeline", 1, "medium", "qa",
     """Code written by model A is reviewed by model B. Writer ≠ reviewer.
From Zenflow, DreamTeam thread: cross-model verification is the highest-signal quality improvement.
Auto-route: if Claude wrote it → Codex reviews. If Codex wrote it → Claude reviews.
Acceptance: every merged task has been reviewed by a different model than the one that wrote it."""),

    ("R10", "lesson-propagation", "Lesson Propagation Between Agents", 1, "medium", "backend",
     """Agents file learnings that subsequent agents automatically inherit.
From czarina: 'Workers file lessons in Hopper; subsequent workers see them automatically'.
Bulletin board: `.sdd/runtime/lessons.jsonl`. Agent A posts 'use xyz pattern for auth'.
Agent B (spawned later) gets this lesson in prompt. Lessons have TTL and confidence score.
Acceptance: lesson filed by agent 1 appears in agent 3's prompt without human intervention."""),

    ("R11", "loop-deadlock-detection", "Agent Loop and Deadlock Detection", 1, "small", "backend",
     """Detect when agents are stuck in loops (editing same file repeatedly) or deadlocked (waiting for each other).
From UiPath/HN Signal #9: 'debugging loops and deadlocks is a nightmare'.
Detect: >3 edits to same file in 5 min = loop. Two agents waiting for each other's locks = deadlock.
Action: kill looping agent, break deadlock by releasing older lock.
Acceptance: stuck agents auto-recovered within 5 minutes."""),

    ("R12", "session-checkpointing-resume", "Session Checkpointing with Auto-Resume", 2, "medium", "backend",
     """Save session state periodically. On crash, resume from last checkpoint instead of starting over.
HN Signal #13: 'long-running agent sequences are especially fragile'.
Checkpoint: task queue, agent state, git state, cost so far. Every 5 minutes or on task completion.
`bernstein resume` picks up from last good state. Acceptance: crash → restart → continues from checkpoint."""),

    ("R13", "complexity-advisor", "Complexity Advisor — Suggest Single Agent When Simpler", 2, "small", "backend",
     """Warn users when task doesn't benefit from multi-agent orchestration.
From Ashpreet Bedi: 'Most teams skip to multi-agent when a single agent with good instructions would suffice'.
Heuristic: scope=small + complexity=low + single-file → suggest single agent mode.
Acceptance: `bernstein -g "fix typo in README"` suggests direct agent instead of full orchestration."""),

    ("R14", "agents-md-aware-routing", "AGENTS.md-Aware Task Routing", 2, "medium", "backend",
     """Read nested AGENTS.md files to auto-configure agent context per subdirectory.
From Nx blog: 'nested AGENTS.md are the default recommendation for monorepo'.
Parse AGENTS.md in each dir → extract rules, conventions, allowed files → inject into agent prompt.
Acceptance: agent working in `frontend/` gets frontend-specific instructions from `frontend/AGENTS.md`."""),

    ("R15", "respect-existing-workflows", "Respect Existing Project Workflows", 2, "small", "backend",
     """Detect and honor existing markdown plans, task systems, and conventions.
From Twitter Signal #34: 'agent keeps getting confused and getting into its own plan mode'.
Detect: existing TODO.md, TASKS.md, .plan files → integrate with Bernstein task system.
Don't override user's workflow — augment it. Acceptance: existing TODO.md items become Bernstein tasks."""),

    ("R16", "goose-adapter", "Goose CLI Agent Adapter", 2, "small", "backend",
     """Add adapter for Block's Goose (30.8K stars). Official roadmap item: Goose wants to be orchestrated.
GitHub goose#6973: 'goose should evolve beyond single chat agent into meta-agent orchestrator'.
Bernstein is that meta-agent orchestrator. This is a strategic partnership opportunity.
Acceptance: `bernstein agents discover` detects Goose. Tasks can route to Goose."""),

    ("R17", "aider-integration", "Deep Aider Integration", 2, "small", "backend",
     """Allow aider users to spawn Bernstein-orchestrated parallel sessions from within aider.
GitHub aider#4428: proposal for multi-agent in aider with /spawn, /delegate commands.
Bernstein provides the orchestration layer aider users are asking for.
Acceptance: aider users can `pip install bernstein` and get parallel agents."""),

    ("R18", "command-allowlist-sandbox", "Command Allowlist/Denylist Per Agent", 2, "medium", "security",
     """Whitelist safe commands per agent role. Block risky operations (rm -rf, DROP TABLE, etc.).
From Martin Alderson: 'innocuous commands like dotnet test can end up doing much more than that'.
Config per role: `backend: {allow: [pytest, ruff, git], deny: [rm -rf, sudo, curl]}`.
Acceptance: agent trying `rm -rf /` is blocked with audit log entry."""),

    ("R19", "token-growth-monitor", "Token Growth Monitor with Auto-Intervention", 2, "small", "backend",
     """Monitor token usage growth per agent. Alert and intervene when conversation grows quadratically.
From Stevens Institute: 'quadratic token growth is the most dangerous economic trap in agent design'.
Track: tokens per turn. If growth rate > 2x per turn for 3 turns → signal agent to summarize and compact.
Acceptance: runaway token growth detected and stopped before 5x budget."""),

    ("R20", "unified-agent-config", "Unified Agent Configuration Manager", 2, "medium", "backend",
     """Single source of truth for rules/skills across Claude Code, Codex, Gemini CLI, Qwen.
From Product Hunt AI Skills Manager: 'frustrated managing skills/rules across 5+ AI coding agents'.
Manage: CLAUDE.md, .codex/instructions.md, .gemini/settings → all from `bernstein.yaml`.
Sync: `bernstein config sync` pushes rules to all installed agents.
Acceptance: one rule change propagates to all agents."""),

    # ── R21-R35: Cost & ROI ──
    ("R21", "real-time-cost-dashboard", "Real-Time Cost Dashboard with Projections", 1, "medium", "backend",
     """Live cost tracking per agent, per task, per session. Project estimated total cost before completion.
Signal #4 (Very High demand): 'Cursor subscription depleted in a single day — $7K gone'.
Show: current spend, projected total, cost per task, cost per agent, cost by model.
Alert thresholds: 50%, 80%, 100% of budget. Acceptance: cost visible in TUI header at all times."""),

    ("R22", "batch-api-integration", "Batch API for 50% Cost Reduction", 1, "small", "backend",
     """Use Anthropic/OpenAI batch APIs for non-latency-sensitive tasks.
50% discount on batch API calls. Route: boilerplate tasks (docs, formatting) to batch.
Config: `batch: {enabled: true, eligible: [docs, style, tests]}`.
Acceptance: batch-eligible tasks cost 50% less than real-time equivalent."""),

    ("R23", "prompt-caching-orchestrator", "Orchestrator-Level Prompt Caching", 1, "medium", "backend",
     """Cache system prompts and project context across agent spawns.
Anthropic: 90% discount on cached input tokens. System prompt (2K tokens) repeated 50x = 45x savings.
Cache key: hash(system_prompt + project_context). Invalidate on file change.
Acceptance: second agent spawn uses cached prompt, 90% cheaper on input tokens."""),

    ("R24", "context-compression-engine", "Context Compression Engine", 1, "medium", "backend",
     """Strip unnecessary context from agent prompts. Only include what's relevant.
From VentureBeat (xMemory): 'prompt itself was only a small portion of tokens — most from system around it'.
Remove: unused tool definitions, irrelevant conversation history, unrelated RAG docs.
Target: <50% context window utilization for small tasks. Acceptance: 30%+ token reduction."""),

    ("R25", "multi-account-rotation", "Multi-Account Credential Rotation", 2, "medium", "backend",
     """Rotate agent sessions across multiple API accounts to maximize rate limits.
From Steve Yegge: 'running three concurrent Claude Max accounts to maintain pace'.
Config: `accounts: [{key: KEY1, provider: anthropic}, {key: KEY2, provider: anthropic}]`.
Round-robin across accounts. Track per-account rate limit state.
Acceptance: 3 accounts → 3x effective rate limit."""),

    ("R26", "roi-dashboard", "ROI Dashboard — Prove Business Value", 1, "medium", "backend",
     """Track developer hours saved, cost per task, agent success rate. Export for budget justification.
From CIO.com: 'budget approvers need numbers — show 376% ROI over 3 years'.
Metrics: tasks_completed × estimated_manual_hours × hourly_rate = value_generated.
Subtract: API costs. Show: net ROI. Acceptance: `bernstein roi` prints: '$X saved, $Y spent, Z% ROI'."""),

    ("R27", "model-downgrade-on-budget", "Auto Model Downgrade at Budget Threshold", 2, "small", "backend",
     """When approaching budget limit, automatically downgrade to cheaper models instead of stopping.
From Signal #12: 'Switching to Opus caused session to burn so quickly'.
At 80% budget: switch opus→sonnet. At 90%: switch sonnet→flash/haiku. At 100%: pause.
Acceptance: budget hit doesn't stop work — it slows down gracefully."""),

    ("R28", "cost-per-line-metric", "Cost Per Line of Code Metric", 2, "small", "backend",
     """Track $/line for each model to identify most cost-efficient providers.
Useful for: model comparison, team reporting, budget planning.
Calculate: total_cost / lines_changed per task. Average by model.
Acceptance: `bernstein cost --per-line` shows cost efficiency by model."""),

    ("R29", "free-tier-maximizer", "Free Tier Maximizer", 2, "small", "backend",
     """Automatically detect and use free tier quotas before paid usage.
Track: remaining free tier per provider. Route tasks to free tier first.
Gemini has generous free tier. Codex has free tier. Use them before Claude paid.
Acceptance: zero-cost tasks routed to free tiers first."""),

    ("R30", "token-budget-per-task", "Token Budget Per Task with Enforcement", 1, "small", "backend",
     """Hard limit on token usage per task. Kill agent at 2x budget.
Signal #1 (High demand): '4 hours of usage gone in 3 prompts'.
Config: `budget: {max_tokens_per_task: {small: 10K, medium: 50K, large: 200K}}`.
Agent prompt includes budget hint. Acceptance: runaway agent killed at budget limit."""),

    ("R31", "cost-attribution-teams", "Cost Attribution by Team/Project", 2, "small", "backend",
     """Tag tasks with team/project for cost allocation.
Config: `cost_center: eng-backend`. `bernstein cost --by-team` shows breakdown.
Export CSV for finance. Acceptance: each task tracked to a cost center."""),

    ("R32", "spend-forecast", "Monthly Spend Forecasting", 2, "medium", "backend",
     """Predict monthly costs from current usage patterns.
Show: 'At current rate, this month will cost $X (±20%)'.
Based on: tasks completed, avg cost per task, remaining tasks.
Acceptance: `bernstein forecast` shows projected monthly cost."""),

    ("R33", "idle-cost-elimination", "Idle Agent Cost Elimination", 2, "small", "backend",
     """Detect agents that are idle (not producing output) and kill them to save cost.
If agent log hasn't grown in 3 minutes and no git changes: assume stuck.
Reclaim slot, requeue task. Acceptance: stuck agents don't waste money."""),

    ("R34", "cost-comparison-report", "Model Cost Comparison Report", 2, "small", "backend",
     """After session: show what it would have cost with different model configurations.
'This run cost $4.50 with opus. With sonnet it would have been $1.20. Quality delta: -5%'.
Useful for: optimizing model routing decisions.
Acceptance: `bernstein recap --cost-alternatives` shows comparison."""),

    ("R35", "invoice-export", "Invoice/Report Export for Clients", 3, "small", "backend",
     """Generate professional cost reports for client billing or internal accounting.
Format: PDF or CSV. Include: date, tasks, models, tokens, cost, duration.
`bernstein report --format pdf --period 2026-03`. Acceptance: produces client-ready document."""),

    # ── R36-R50: Quality & Trust ──
    ("R36", "quality-first-mode", "Quality-First Orchestration Mode", 1, "medium", "backend",
     """Fewer agents, deeper review, mutation testing, multi-model verification.
HN Signal #20 (79 points): 'Is anyone exploring quality vs quantity? Low quality * more commits is useless'.
Mode: `bernstein run --quality-first`. 2 agents max. Each output: mutation tested + cross-model reviewed.
Acceptance: quality-first mode produces 50%+ fewer but significantly better commits."""),

    ("R37", "mutation-testing-gate", "Mutation Testing Quality Gate", 1, "medium", "qa",
     """Run mutation testing on agent-written tests. Block merge if mutation score < threshold.
From Diffblue: '81% line coverage but only 61% mutation coverage — tests don't catch real bugs'.
Use mutmut or cosmic-ray. Config: `quality_gates: {mutation: {min_score: 60}}`.
Acceptance: agent writes test → mutation testing runs → low score blocks merge."""),

    ("R38", "tiered-watchdog", "3-Tier Watchdog System", 1, "medium", "backend",
     """Mechanical health checks → AI triage → human escalation.
From overstory: 'compounding error rates, cost amplification, debugging complexity are the NORMAL case'.
Tier 1: daemon checks (process alive, log growing). Tier 2: AI reviews output quality.
Tier 3: human notified for complex issues. Acceptance: problems caught before reaching main."""),

    ("R39", "output-review-assistant", "Agent Output Review Assistant", 2, "medium", "backend",
     """Smart diff summaries and change-impact analysis for agent output.
HN Signal #16: 'copious amounts of code make review way harder than small snippets'.
Generate: 'Agent changed auth.py: added JWT refresh. Impact: 3 tests updated. Risk: low'.
Acceptance: every task gets a human-readable review summary."""),

    ("R40", "state-machine-workflow", "Explicit State Machine Workflow Mode", 2, "medium", "backend",
     """Define workflow as states: plan→implement→test→review→merge with deterministic transitions.
HN Signal #25: 'I'm driving a deterministic state machine per code ticket — quality is very good'.
Config in YAML: define states, transitions, gates between states.
Acceptance: workflow enforces test-before-merge regardless of agent behavior."""),

    ("R41", "test-agent-slot", "Dedicated Test Agent Slot", 1, "small", "backend",
     """Always spawn a test-writing agent alongside implementation agents.
From Diffblue/Meta JiTTests: dedicated test agent dramatically improves coverage.
Config: `test_agent: {always_spawn: true, model: sonnet, trigger: on_task_complete}`.
Acceptance: every implementation task triggers a paired test-writing task."""),

    ("R42", "code-quality-scoring", "Per-Task Code Quality Score", 2, "small", "qa",
     """Score each agent's output: test pass rate, lint errors, complexity delta, line count.
Score 0-100, stored per task. Historical tracking: quality trend.
Feed into routing: prefer agents with higher scores for complex tasks.
Acceptance: `bernstein recap` shows quality score per task."""),

    ("R43", "semantic-conflict-detection", "Semantic Conflict Detection", 2, "medium", "backend",
     """Detect semantic conflicts (not just git conflicts) between agent branches.
Two agents both modify auth but in incompatible ways — git won't catch this.
Use AST analysis: 'Agent A added param X to function F. Agent B renamed function F'.
Acceptance: semantic conflicts flagged before merge attempt."""),

    ("R44", "regression-detection-gate", "Regression Detection Quality Gate", 1, "small", "qa",
     """Run performance benchmarks before/after agent changes. Block merge on regression.
Config: `quality_gates: {benchmark: {command: 'pytest benchmarks/', threshold: 10%}}`.
Compare: response time, memory usage, throughput.
Acceptance: 15% performance regression → merge blocked with explanation."""),

    ("R45", "golden-test-suite", "Golden Test Suite for Orchestrator", 2, "medium", "qa",
     """20+ curated tasks with known-good solutions. Verify orchestrator doesn't regress.
Run on every release. Compare: success rate, cost, quality across versions.
`bernstein eval golden` runs all. CI: golden suite in release pipeline.
Acceptance: quality regression detected before release."""),

    ("R46", "pii-detection-gate", "PII Detection in Agent Output", 1, "small", "security",
     """Scan agent output for PII (emails, API keys, passwords, phone numbers) before merge.
From Cursor/NVIDIA: 'environment variable leakage is the biggest blind spot'.
Use regex patterns + optional LLM verification for edge cases.
Acceptance: agent accidentally including API key in comment → merge blocked."""),

    ("R47", "agent-output-size-limit", "Agent Output Size Limit", 2, "small", "backend",
     """Reject agent output that changes too many files or too many lines.
Heuristic: small task → max 5 files, 200 lines. Large task → max 20 files, 1000 lines.
If exceeded: reject with 'output exceeds scope — decompose into subtasks'.
Acceptance: agent that rewrites entire codebase is blocked."""),

    ("R48", "deterministic-reproducibility", "Deterministic Run Reproducibility", 2, "large", "backend",
     """Given same seed + same codebase → produce same task decomposition every time.
Record: all random seeds, model responses (for replay), routing decisions.
`bernstein replay <session_id>` reproduces exact same run.
Acceptance: two runs with same inputs produce identical task plans."""),

    ("R49", "chaos-test-suite", "Chaos Engineering Test Suite", 2, "medium", "qa",
     """Inject faults to verify resilience: agent crash, timeout, OOM, merge conflict, disk full.
`bernstein test --chaos` enables fault injection.
Verify: tasks retry, slots reclaimed, no data loss.
Acceptance: all fault scenarios handled gracefully."""),

    ("R50", "swe-bench-integration", "SWE-Bench Benchmark Integration", 2, "large", "qa",
     """Run SWE-Bench (real GitHub issues) through Bernstein. Publish results on leaderboard.
This is THE benchmark for coding agents. Publishing results = instant credibility.
`bernstein eval swe-bench --subset lite`. Per-model results: resolve rate, cost, time.
Acceptance: SWE-Bench Lite results published with Bernstein orchestration numbers."""),

    # ── R51-R65: Enterprise Ready ──
    ("R51", "soc2-audit-export", "SOC 2 Evidence Export Package", 1, "medium", "security",
     """One-command export of all audit data for SOC 2 auditors.
`bernstein audit export --period Q1-2026 --format zip`. Contains: audit logs, access records, policy versions.
From PolicyLayer: 'VPs of Engineering ask for proof of what agents do during procurement'.
Acceptance: auditor receives complete evidence package."""),

    ("R52", "tenant-isolation", "Multi-Tenant Workspace Isolation", 2, "large", "backend",
     """Isolated workspaces per team. Separate task queues, configs, budgets, audit trails.
Config: `tenants: [{id: team-a, budget: 100, agents: [claude, codex]}]`.
No cross-tenant data leakage. Per-tenant admin UI.
Acceptance: team A cannot see team B's tasks or costs."""),

    ("R53", "approval-workflow-engine", "Approval Workflow Engine", 1, "medium", "backend",
     """Configurable approval gates per task type: auto-approve low-risk, require human for high-risk.
From enterprise research: 'human-in-the-loop is the #6 most important enterprise feature'.
Config: `approval: {high_risk: require_human, low_risk: auto, medium_risk: ai_review}`.
Notification: Slack/email when approval needed. Timeout: auto-reject after 24h.
Acceptance: high-risk task waits for human approval before merge."""),

    ("R54", "api-key-rotation", "Automatic API Key Rotation", 2, "small", "security",
     """Rotate API keys on schedule. Detect compromised keys and revoke immediately.
Config: `key_rotation: {interval: 30d, on_leak: revoke_immediately}`.
Integration with secrets managers (Vault, AWS, 1Password).
Acceptance: keys rotated monthly without service interruption."""),

    ("R55", "data-residency-routing", "Data Residency-Aware Model Routing", 2, "medium", "backend",
     """Route tasks to models in specific regions for GDPR/data residency compliance.
Config: `data_residency: {region: eu, providers: [anthropic-eu, azure-eu]}`.
EU data stays in EU. Acceptance: no data crosses configured region boundaries."""),

    ("R56", "agent-identity-lifecycle", "Agent Identity Lifecycle Management", 2, "medium", "security",
     """Agents get first-class identities: create, authenticate, authorize, audit, revoke.
From NIST AI Agent Standards: agents need autonomous identities, not recycled user creds.
Each agent session: unique identity, scoped permissions, full audit trail.
Acceptance: `bernstein agents list --identities` shows all agent identities."""),

    ("R57", "compliance-presets", "Compliance Presets (SOC2, HIPAA, GDPR, FedRAMP)", 2, "medium", "security",
     """One-command compliance configuration. `bernstein init --compliance soc2`.
Sets: audit logging, data retention, access controls, encryption.
Preset validates: are all required controls in place?
Acceptance: `bernstein doctor --compliance soc2` shows compliance checklist."""),

    ("R58", "enterprise-sla-dashboard", "SLA Dashboard with Error Budgets", 2, "medium", "backend",
     """Define SLOs: task success rate ≥ 90%, merge success ≥ 95%, p95 duration < 30min.
Track error budget. When exhausted: reduce parallelism, notify owner.
Dashboard: SLO status, burn rate, time to budget exhaustion.
Acceptance: SLA violations visible in real-time."""),

    ("R59", "change-risk-scoring", "Automatic Change Risk Scoring", 1, "medium", "backend",
     """Score each task's risk level: blast radius, reversibility, uncertainty.
From modern change management: classify changes by risk, apply appropriate rigor.
High risk: more tests, ADR required, staged rollout. Low risk: fast path.
Auto-detect: touches auth/billing/infra → high risk. Touches docs/tests → low risk.
Acceptance: every task has a risk score that influences verification depth."""),

    ("R60", "guardrails-integration", "Guardrails AI Integration", 2, "medium", "security",
     """Integrate Guardrails AI (open source) for input/output validation.
Check: prompt injection, PII leakage, code injection, behavioral boundaries.
Config: `guardrails: {enabled: true, rules: [no_pii, no_eval, no_exec]}`.
Acceptance: malicious agent prompt → blocked with audit log."""),

    ("R61", "policy-as-code", "Policy-as-Code Engine (OPA/Rego)", 2, "large", "security",
     """Define organizational policies in OPA/Rego. Engine enforces at task creation and output validation.
Policies in `.sdd/policies/`. Example: `deny { input.files_changed > 20 }`.
`bernstein policy check` runs manual audit. Violations block merge.
Acceptance: policy violation → merge blocked with explanation."""),

    ("R62", "saml-sso", "SAML/OIDC SSO Enterprise Integration", 2, "medium", "security",
     """Login via company IdP (Okta, Azure AD, Google Workspace).
OIDC authorization code flow for CLI. SAML for web dashboard.
`bernstein login --sso` initiates browser flow. Token cached.
Acceptance: enterprise user logs in with company credentials."""),

    ("R63", "usage-metering-api", "Usage Metering API for Billing", 3, "medium", "backend",
     """REST API for usage data: tasks, tokens, cost, duration per team/project.
For: internal chargeback, client billing, SaaS metering.
`GET /usage?team=eng&period=2026-03` returns structured data.
Acceptance: finance team can pull usage data programmatically."""),

    ("R64", "deployment-modes", "Cloud/VPC/On-Prem Deployment Modes", 2, "large", "backend",
     """Flexible deployment: local (default), Docker Compose, Kubernetes Helm chart, air-gapped.
From enterprise research: 'deployment flexibility is a hard procurement requirement'.
`bernstein deploy --mode docker` generates docker-compose.yml.
Helm chart in `deploy/helm/`. Air-gapped: bundled with local models.
Acceptance: each deployment mode documented and tested."""),

    ("R65", "white-label-api", "White-Label API for Resellers", 3, "medium", "backend",
     """Remove Bernstein branding. Custom domain, custom logo, custom name.
Config: `branding: {name: "AgentOps", logo: "logo.png", domain: "agentops.example.com"}`.
For: consulting firms, managed service providers, enterprise OEM.
Acceptance: no 'Bernstein' visible in white-labeled deployment."""),

    # ── R66-R80: Ecosystem & Moat ──
    ("R66", "mcp-server-mode", "Expose Bernstein as MCP Server", 1, "medium", "backend",
     """Other tools can orchestrate agents through Bernstein via MCP protocol.
From Forrester: '30% of enterprise app vendors will launch MCP servers in 2026'.
Bernstein as MCP server: create_task, list_tasks, get_status tools.
Acceptance: Claude Code can use Bernstein as an MCP tool."""),

    ("R67", "a2a-protocol-support", "Agent-to-Agent Protocol (A2A) Support", 2, "medium", "backend",
     """Implement Google's A2A protocol for inter-agent communication.
From ThoughtWorks Radar: 'A2A and AG-UI are reducing boilerplate for multi-agent apps'.
Bernstein agents can discover and communicate with external A2A agents.
Acceptance: Bernstein agent sends message to external A2A agent and receives response."""),

    ("R68", "plugin-marketplace", "Community Plugin Marketplace", 2, "medium", "backend",
     """Discover, install, rate community plugins. `bernstein plugins search "auth"`.
Registry: JSON index on GitHub or npm-style registry.
Quality: stars, downloads, verified badge. Install: `bernstein plugins install foo`.
Acceptance: 5+ community plugins published and discoverable."""),

    ("R69", "task-template-sharing", "Community Task Template Sharing", 2, "small", "backend",
     """Share/download pre-built task templates. 'refactor+test+review' pipeline.
`bernstein templates install https://github.com/user/bernstein-templates`.
Curated gallery on docs site. Acceptance: 10+ community templates available."""),

    ("R70", "ci-feedback-loop", "CI Failure → Auto-Fix Feedback Loop", 1, "medium", "backend",
     """Route CI failures back to responsible agents. Auto-trigger fix cycles.
From Elastic: 'CI system is the scheduler — agents auto-fix failing dependency PRs'.
On CI failure: parse error, create fix task, assign to agent, re-run CI.
Acceptance: CI failure → auto-fix task → green CI, no human intervention."""),

    ("R71", "github-app-v2", "GitHub App v2 — Issues, PRs, Comments", 1, "medium", "backend",
     """Full GitHub integration: issues → tasks, PR review, auto-comment on PR.
Failed tasks → GitHub issues. Completed tasks → PR with description.
Webhook: push/PR/issue events → Bernstein tasks.
Acceptance: opening GitHub issue labeled 'bernstein' creates task automatically."""),

    ("R72", "slack-bot-full", "Full Slack Bot Integration", 2, "medium", "backend",
     """Create tasks, get status, approve via Slack. `/bernstein status` in any channel.
Slash commands: `/bernstein run "goal"`, `/bernstein cost`, `/bernstein approve <id>`.
Notifications: task complete, approval needed, budget alert.
Acceptance: full orchestration workflow possible from Slack."""),

    ("R73", "vscode-extension-v2", "VS Code Extension v2 — Sidebar Dashboard", 2, "medium", "backend",
     """Webview panel: task list with status badges, agent logs, cost tracker, approve button.
Status bar: live cost, active agents. Command palette: Run, Stop, Status.
Acceptance: full Bernstein workflow from within VS Code."""),

    ("R74", "jetbrains-plugin", "JetBrains Plugin (IntelliJ, PyCharm)", 3, "large", "backend",
     """Tool window: task dashboard, agent logs. Run configuration. Cost tracker in status bar.
Diff view for agent changes. Acceptance: run Bernstein from PyCharm."""),

    ("R75", "neovim-integration", "Neovim Plugin (Telescope/Lua)", 3, "small", "backend",
     """Telescope picker for tasks. Floating window for agent status. Keymap for approve/reject.
Lua plugin, minimal dependencies. Acceptance: neovim users can orchestrate agents."""),

    ("R76", "terraform-provider", "Terraform Provider for Bernstein", 3, "medium", "backend",
     """Manage Bernstein infrastructure as code. `resource "bernstein_workspace" "main" {}`.
For: enterprise GitOps workflows. Acceptance: Terraform apply creates workspace."""),

    ("R77", "helm-chart", "Production Helm Chart for Kubernetes", 2, "medium", "backend",
     """Deploy Bernstein on K8s: server, spawner as separate pods. Auto-scaling.
`helm install bernstein ./deploy/helm`. ConfigMap for bernstein.yaml.
Acceptance: production deployment on K8s with 3-command setup."""),

    ("R78", "docker-compose-prod", "Production Docker Compose Template", 2, "small", "backend",
     """One-command deployment: `docker compose up`. Server + Redis + optional PostgreSQL.
Pre-configured for: self-hosted, demo, development modes.
Acceptance: `docker compose up` → working Bernstein in 30 seconds."""),

    ("R79", "sdk-python-v2", "Bernstein Python SDK v2 — Typed Client", 2, "medium", "backend",
     """`from bernstein import Client; c = Client(); c.create_task(...)`.
Typed responses (Pydantic models). Async variant. Auth support.
100% API coverage. Published to PyPI as `bernstein-sdk`.
Acceptance: external Python app can manage Bernstein programmatically."""),

    ("R80", "sdk-typescript", "Bernstein TypeScript SDK", 3, "medium", "backend",
     """`import { BernsteinClient } from 'bernstein-sdk'`. For Node.js/Deno integrations.
Typed with TypeScript. Async/await. Published to npm.
Acceptance: TypeScript app can manage Bernstein via SDK."""),

    # ── R81-R90: Thought Leadership ──
    ("R81", "public-benchmarks", "Public Benchmark Leaderboard", 1, "medium", "backend",
     """Publish reproducible benchmarks: Bernstein vs manual, vs single-agent, vs competitors.
Metrics: tasks completed, cost, quality, time. Refreshed monthly.
Host on docs site. This is the DEMO that goes viral.
Acceptance: benchmark page with reproducible numbers, shareable link."""),

    ("R82", "live-demo-playground", "Live Demo Playground (Web)", 2, "large", "backend",
     """Web-based playground: paste a GitHub repo URL → watch Bernstein orchestrate in real-time.
No install needed. Free tier: 1 run/day, max 3 tasks.
Hosted on Vercel/Fly.io. Acceptance: anyone can try Bernstein in browser."""),

    ("R83", "conference-demo-kit", "Conference Demo Kit", 2, "small", "backend",
     """Pre-recorded demos, slide templates, talking points for conference talks.
`bernstein demo --conference` runs a polished 5-minute demo with commentary.
Materials in `docs/conference/`. Acceptance: speaker-ready materials for 3 talk formats."""),

    ("R84", "blog-post-generator", "Technical Blog Post Generator", 3, "small", "backend",
     """`bernstein blog "How Bernstein saved us $500/month on AI coding"` generates blog draft.
Uses session data: tasks, costs, before/after. Markdown output.
For: content marketing, case studies. Acceptance: generates publishable draft."""),

    ("R85", "benchmark-against-crewai", "Head-to-Head Benchmark vs CrewAI/LangGraph", 1, "medium", "qa",
     """Run identical tasks through Bernstein and competitors. Publish comparison.
Metrics: cost (Bernstein should win — no LLM overhead on scheduling), reliability, quality.
Methodology: open, reproducible, fair. Acceptance: published comparison with methodology."""),

    ("R86", "agentic-engineering-manifesto", "Agentic Engineering Manifesto Document", 2, "small", "backend",
     """Write and publish 'The Agentic Engineering Manifesto' — defining the discipline.
Position Alex as the thought leader who named and defined the practice.
Principles: deterministic scheduling, verified output, provider-agnostic, human oversight.
Publish on alexchernysh.com + Medium + Dev.to. Acceptance: published, shareable."""),

    ("R87", "open-dataset-agent-traces", "Open Dataset of Agent Execution Traces", 3, "medium", "backend",
     """Publish anonymized traces of agent task execution for research community.
Data: task descriptions, model used, success/fail, cost, duration, quality score.
Format: JSONL on HuggingFace. Acceptance: dataset published, cited by researchers."""),

    ("R88", "contributor-program", "Contributor Recognition Program", 2, "small", "backend",
     """Automated contributor recognition: leaderboard, badges, swag for top contributors.
CONTRIBUTORS.md auto-generated from git history. Monthly spotlight.
Discord role upgrades for contributors. Acceptance: program documented and active."""),

    ("R89", "case-study-templates", "Customer Case Study Templates", 3, "small", "backend",
     """Templates for documenting real-world Bernstein usage. Problem → solution → results format.
Auto-fill from session data: tasks, cost, time saved.
For: website testimonials, sales collateral. Acceptance: 3 case study templates."""),

    ("R90", "youtube-channel-content", "YouTube Content Pipeline", 3, "small", "backend",
     """Script templates for YouTube content: tutorials, demos, deep dives.
Automated recording: `bernstein record` captures terminal session as video-ready content.
Content calendar in docs/content-calendar.md. Acceptance: 5 video scripts ready."""),

    # ── R91-R100: Moonshots ──
    ("R91", "autonomous-ops-agent", "Autonomous Ops Agent — Monitor Prod, Fix On Failure", 2, "large", "backend",
     """Agent monitors production, creates fix tasks on failure, deploys fix.
`bernstein watch --prod` monitors health endpoint. On 5xx spike → create fix task → agent fixes → PR.
Human approves PR. If approved → deploy. Acceptance: production incident → auto-fix PR in <30min."""),

    ("R92", "codebase-knowledge-graph", "Codebase Knowledge Graph", 2, "large", "backend",
     """Build a graph of code relationships for better task decomposition.
Nodes: files, functions, classes, modules. Edges: imports, calls, inherits, references.
Use for: impact analysis, scope estimation, intelligent file locking.
`bernstein graph impact "auth.py"` shows affected files. Acceptance: graph built, queryable."""),

    ("R93", "self-improving-quality-gates", "Self-Improving Quality Gates", 3, "large", "backend",
     """Quality gates learn from what they catch. If mutation testing catches 0 bugs in 50 runs → reduce frequency.
If PII detection catches real PII → increase sensitivity. Adaptive thresholds.
Acceptance: gate configurations auto-tune based on historical data."""),

    ("R94", "multi-repo-orchestration-v2", "Multi-Repo Orchestration v2", 2, "large", "backend",
     """Coordinate tasks across multiple git repos. Shared task server, per-repo agents.
Cross-repo dependencies: 'update API schema in backend AND frontend'.
Merge coordination: don't merge frontend until backend dependency merged.
Acceptance: 2 repos, cross-repo task, coordinated merge."""),

    ("R95", "natural-language-queries", "Natural Language Codebase Queries", 2, "medium", "backend",
     """`bernstein ask "how does auth work?"` answers from codebase knowledge.
Uses RAG index + knowledge graph. No cloud call for simple queries.
`bernstein ask "which agent is working on tests?"` answers from runtime state.
Acceptance: NL queries return accurate answers about code and runtime."""),

    ("R96", "agent-swarm-protocol", "Agent Swarm Coordination Protocol", 3, "large", "backend",
     """Emergent task solving from simple agent rules. Agents self-organize without central scheduling.
Rules: 'claim unclaimed tasks matching your skills', 'review adjacent agents' work'.
Self-balancing: agents naturally distribute work based on capability and availability.
Acceptance: 10 agents self-organize to complete 20 tasks without explicit assignment."""),

    ("R97", "predictive-bug-detection", "Predictive Bug Detection", 3, "large", "backend",
     """Identify likely bugs before they manifest. ML model trained on past bug patterns.
Features: code complexity, churn rate, test coverage, agent confidence.
Predict: 'this change has 73% probability of introducing a bug in auth module'.
Acceptance: prediction accuracy > 60% on historical data."""),

    ("R98", "continuous-deployment-agent", "Continuous Deployment Agent", 3, "large", "backend",
     """Agents deploy to staging, run smoke tests, promote to production.
Pipeline: merge → build → deploy staging → test → promote prod → monitor.
Rollback: if monitoring shows regression → auto-rollback.
Acceptance: agent-driven deployment with automatic rollback."""),

    ("R99", "federated-learning-agents", "Federated Learning Across Agent Instances", 3, "large", "backend",
     """Share learnings across Bernstein instances without sharing code.
Aggregate: 'opus works well for auth tasks' from 100 instances → global routing improvement.
Privacy: only aggregate stats shared, never code or prompts.
Acceptance: routing improves from aggregated cross-instance data."""),

    ("R100", "bernstein-cloud-saas", "Bernstein Cloud — Hosted SaaS", 2, "large", "backend",
     """Hosted Bernstein: sign up → connect GitHub → set goal → agents work.
Free tier: 5 tasks/day, $0 budget. Pro: unlimited, $29/month. Enterprise: custom.
Multi-tenant, managed infrastructure, billing integration.
This is the revenue endgame. Acceptance: paying customer uses hosted Bernstein."""),
]


def main() -> None:
    count = 0
    for tid, slug, title, priority, scope, role, body in TICKETS:
        path = BACKLOG / f"{tid}-{slug}.md"
        body_clean = body.strip()
        content = f"""# {tid} — {title}

**Priority:** {priority}
**Scope:** {scope}
**Role:** {role}

{body_clean}
"""
        path.write_text(content, encoding="utf-8")
        count += 1
    print(f"Generated {count} tickets in {BACKLOG}")


if __name__ == "__main__":
    main()
