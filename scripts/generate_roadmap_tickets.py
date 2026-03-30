#!/usr/bin/env python3
"""Generate 250 roadmap tickets for Bernstein's future development.

Optimized for execution by Bernstein's own agents:
- Each ticket is self-contained (agent can pick up without context)
- Scope/complexity rated for routing (small→sonnet, large→opus)
- Dependencies explicit where needed
- Acceptance criteria machine-verifiable where possible
"""

import textwrap
from pathlib import Path

BACKLOG = Path(".sdd/backlog/open")
BACKLOG.mkdir(parents=True, exist_ok=True)

# =============================================================================
# DETAILED TICKETS (100) — Waves 6-13
# =============================================================================

DETAILED = [
    # ── W6: Enterprise & Compliance (15) ──
    ("W6-01", "enterprise-soc2-audit-mode", "backend", 1, "medium", "high", """
# W6-01 — SOC 2 Audit Mode

Enable SOC 2 compliance logging. Every agent action logged to append-only audit trail with cryptographic seals.

## Acceptance Criteria
1. `bernstein run --audit` enables audit mode
2. Every task create/claim/complete/fail event logged to `.sdd/audit/audit.jsonl`
3. Each entry: {timestamp, actor, action, resource, input_hash, output_hash}
4. Merkle tree seal generated on shutdown (`bernstein audit seal`)
5. `bernstein audit verify` validates chain integrity
6. Unit tests for seal generation and verification
"""),
    ("W6-02", "rbac-role-based-access", "backend", 1, "large", "high", """
# W6-02 — Role-Based Access Control (RBAC)

Multi-user RBAC for team deployments. Control who can create tasks, approve plans, view costs.

## Acceptance Criteria
1. Roles: admin, developer, viewer
2. Admin: full access. Developer: create tasks, view all. Viewer: read-only
3. Auth via JWT tokens with role claims
4. API endpoints enforce role checks via middleware
5. `bernstein users add/remove/list` CLI commands
6. Config in `.sdd/config/rbac.yaml`
"""),
    ("W6-03", "gdpr-data-retention", "backend", 2, "medium", "medium", """
# W6-03 — GDPR Data Retention Policies

Configurable data retention with automatic purge. Agent logs, task data, cost records expire after N days.

## Acceptance Criteria
1. Config: `retention: {logs: 30d, tasks: 90d, costs: 365d}`
2. Daily cleanup job (cron or startup) purges expired data
3. `bernstein purge --dry-run` shows what would be deleted
4. No PII stored in task descriptions (sanitize on create)
"""),
    ("W6-04", "sso-oidc-integration", "backend", 2, "medium", "high", """
# W6-04 — SSO via OIDC/SAML

Enterprise SSO integration. Login via company IdP (Okta, Azure AD, Google Workspace).

## Acceptance Criteria
1. OIDC authorization code flow for CLI login
2. SAML assertion parsing for enterprise IdPs
3. `bernstein login --sso` initiates browser-based flow
4. Token cached in `~/.bernstein/token.json` with refresh
5. Works with Okta, Azure AD, Google (test with mock IdP)
"""),
    ("W6-05", "ip-allowlist-network-policy", "security", 2, "small", "low", """
# W6-05 — IP Allowlist / Network Policy

Restrict task server access to allowed IP ranges for network-isolated deployments.

## Acceptance Criteria
1. Config: `network: {allowed_ips: ["10.0.0.0/8", "192.168.1.0/24"]}`
2. Middleware rejects requests from non-allowed IPs with 403
3. Localhost (127.0.0.1) always allowed
4. `bernstein doctor` reports network policy status
"""),
    ("W6-06", "compliance-report-generator", "backend", 2, "medium", "medium", """
# W6-06 — Compliance Report Generator

Auto-generate compliance reports from audit trail for SOC 2 / ISO 27001 auditors.

## Acceptance Criteria
1. `bernstein report compliance --format pdf --period 2026-Q1`
2. Report sections: access events, agent actions, error rates, data retention proof
3. PDF output via reportlab or weasyprint (optional dep)
4. JSON output always available
"""),
    ("W6-07", "secrets-manager-integration", "security", 1, "medium", "medium", """
# W6-07 — Secrets Manager Integration

Load API keys from external secrets managers instead of environment variables.

## Acceptance Criteria
1. Support: AWS Secrets Manager, HashiCorp Vault, 1Password CLI
2. Config: `secrets: {provider: vault, path: "secret/bernstein"}`
3. Keys loaded at startup, refreshed on TTL expiry
4. Fallback to env vars if secrets manager unavailable
5. `bernstein doctor` checks secrets manager connectivity
"""),
    ("W6-08", "agent-sandboxing-containers", "backend", 2, "large", "high", """
# W6-08 — Container-Based Agent Sandboxing

Run agents in Docker/Podman containers for full isolation. Prevent agents from accessing host filesystem.

## Acceptance Criteria
1. `bernstein run --sandbox docker` spawns agents in containers
2. Each agent gets: project dir (bind mount, read-write), no host network, no host filesystem
3. Container image configurable per adapter
4. Fallback to worktree isolation when Docker unavailable
5. Resource limits: CPU, memory, disk via cgroups
"""),
    ("W6-09", "change-management-approval-flow", "backend", 2, "medium", "high", """
# W6-09 — Change Management Approval Flow

Multi-stage approval for high-risk changes. Manager review → security review → merge.

## Acceptance Criteria
1. Tasks with risk_level=high require 2 approvals before merge
2. Approval via `bernstein approve <task_id>` or TUI button
3. Slack/email notification when approval needed
4. Timeout: auto-reject after 24h with no approval
5. Audit trail: who approved, when, with what comment
"""),
    ("W6-10", "cost-allocation-department-tags", "backend", 3, "small", "low", """
# W6-10 — Cost Allocation with Department Tags

Tag tasks with department/project codes for cost allocation in enterprise billing.

## Acceptance Criteria
1. Task field: `cost_center: "eng-backend"`
2. `bernstein cost --by-department` shows cost breakdown by tag
3. Export to CSV for finance team
4. Config: `cost_centers: [eng-backend, eng-frontend, qa, security]`
"""),
    ("W6-11", "eu-ai-act-risk-assessment", "security", 2, "medium", "high", """
# W6-11 — EU AI Act Risk Assessment

Automated risk classification per EU AI Act requirements. Log risk assessments for each agent session.

## Acceptance Criteria
1. Classify agent tasks by EU AI Act risk level (minimal, limited, high, unacceptable)
2. High-risk tasks require human oversight before merge
3. Risk assessment logged in audit trail
4. `bernstein compliance eu-ai-act` shows current status
"""),
    ("W6-12", "disaster-recovery-backup", "backend", 2, "medium", "medium", """
# W6-12 — Disaster Recovery: State Backup/Restore

Backup .sdd/ state to remote storage. Restore from backup on new machine.

## Acceptance Criteria
1. `bernstein backup --to s3://bucket/path` or `--to ./backup.tar.gz`
2. `bernstein restore --from s3://bucket/path`
3. Includes: backlog, config, audit trail, metrics. Excludes: runtime, logs
4. Encrypted backup option: `--encrypt`
5. Scheduled backup via cron config
"""),
    ("W6-13", "policy-as-code-engine", "backend", 2, "large", "high", """
# W6-13 — Policy-as-Code Engine

Define organizational policies in YAML/Rego. Engine enforces at task creation and agent output validation.

## Acceptance Criteria
1. Policies defined in `.sdd/policies/` as YAML files
2. Example: `no_eval: {rule: "file_content must not contain 'eval('", severity: block}`
3. Policy evaluated before merge (like quality gates but configurable)
4. `bernstein policy check` runs manual audit
5. Policy violations logged in audit trail
"""),
    ("W6-14", "data-classification-labels", "security", 3, "small", "low", """
# W6-14 — Data Classification Labels

Tag repositories and tasks with data sensitivity levels for compliance.

## Acceptance Criteria
1. Labels: public, internal, confidential, restricted
2. Agents working on restricted repos get extra isolation
3. Restricted tasks can't be delegated to cloud agents (local-only)
4. Classification in bernstein.yaml: `data_classification: confidential`
"""),
    ("W6-15", "immutable-audit-log-blockchain", "security", 3, "medium", "high", """
# W6-15 — Immutable Audit Log with Hash Chain

Tamper-evident audit log using hash chains (not blockchain, just linked hashes).

## Acceptance Criteria
1. Each audit entry includes hash of previous entry (chain)
2. `bernstein audit verify` detects any tampered entries
3. Export chain to external verification (JSON with hashes)
4. Performance: <1ms per entry append
"""),

    # ── W7: Developer Experience (15) ──
    ("W7-01", "persistent-memory-across-sessions", "backend", 1, "large", "high", """
# W7-01 — Persistent Memory Across Sessions

Agents remember decisions, conventions, and learnings from previous sessions.

## Acceptance Criteria
1. SQLite-backed memory store in `.sdd/memory/memory.db`
2. Types: convention (code style), decision (architectural choice), learning (what worked/failed)
3. Agents receive relevant memories in their prompt context
4. Memory pruning: old/irrelevant memories decay over time
5. `bernstein memory list/add/remove` CLI
6. Privacy: memory never sent to third parties
"""),
    ("W7-02", "side-by-side-branch-diff", "backend", 1, "medium", "medium", """
# W7-02 — Side-by-Side Branch Diff Comparison

Compare what different agents did for the same task (parallel branch comparison).

## Acceptance Criteria
1. `bernstein diff --compare agent1 agent2` shows side-by-side diff
2. TUI: select two completed tasks, see diff in split view
3. Pick best solution: `bernstein merge --pick agent1`
4. Useful for A/B testing different models on same task
"""),
    ("W7-03", "task-templates-library", "backend", 2, "small", "low", """
# W7-03 — Task Templates Library

Pre-built task templates for common workflows. Reduce goal→task decomposition time.

## Acceptance Criteria
1. Templates in `.sdd/templates/`: refactor, add-tests, fix-bug, add-feature, security-audit
2. `bernstein run --template add-tests` uses template instead of goal decomposition
3. Templates are YAML with task list, roles, dependencies
4. `bernstein templates list` shows available templates
5. Community templates: `bernstein templates install <url>`
"""),
    ("W7-04", "natural-language-status-queries", "backend", 2, "medium", "medium", """
# W7-04 — Natural Language Status Queries

Ask questions about the current run in natural language.

## Acceptance Criteria
1. `bernstein ask "which agent is working on tests?"` → answers from task server state
2. `bernstein ask "how much have we spent?"` → cost summary
3. `bernstein ask "why did task 3 fail?"` → agent logs excerpt
4. Uses local LLM or simple pattern matching (no cloud calls for queries)
5. TUI: press `/` to enter query mode
"""),
    ("W7-05", "undo-rollback-mechanism", "backend", 1, "medium", "high", """
# W7-05 — Undo / Rollback Mechanism

Revert an agent's merged changes if they cause problems.

## Acceptance Criteria
1. `bernstein undo <task_id>` creates a revert commit for that task's changes
2. `bernstein undo --all` reverts entire session's changes
3. Before undo: show diff of what will be reverted
4. Safety: run tests after revert to verify clean state
5. Audit trail: log who reverted and why
"""),
    ("W7-06", "interactive-task-editor", "backend", 2, "medium", "medium", """
# W7-06 — Interactive Task Editor

Edit tasks mid-flight. Change priority, reassign agent, modify description.

## Acceptance Criteria
1. TUI: select task, press `e` to edit
2. Editable fields: title, description, priority, role, model
3. Changes applied immediately (task server updated)
4. If agent is already working: signal to re-read task
5. `bernstein edit <task_id>` opens in $EDITOR
"""),
    ("W7-07", "project-type-presets", "backend", 2, "small", "low", """
# W7-07 — Project Type Presets

Auto-configure bernstein.yaml based on detected project type.

## Acceptance Criteria
1. Detection: Python (pyproject.toml), Node (package.json), Rust (Cargo.toml), Go (go.mod)
2. Per-type defaults: test command, lint command, build command, quality gates
3. `bernstein init` uses detection to pre-fill config
4. Overridable: user can customize after detection
"""),
    ("W7-08", "smart-context-injection", "backend", 1, "large", "high", """
# W7-08 — Smart Context Injection

Automatically include relevant files in agent prompt based on task content.

## Acceptance Criteria
1. For each task: identify relevant files via RAG index search
2. Inject top-5 relevant files as context in agent prompt
3. Configurable: `context: {max_files: 10, max_tokens: 50000}`
4. Priority: files mentioned in task description > recently modified > high-centrality
5. Benchmark: measure task success rate with/without context injection
"""),
    ("W7-09", "agent-replay-debugging", "backend", 2, "medium", "medium", """
# W7-09 — Agent Replay Debugging

Replay a failed agent session with modified prompt or different model.

## Acceptance Criteria
1. `bernstein replay <task_id>` re-runs the exact same task
2. `bernstein replay <task_id> --model opus` replays with different model
3. `bernstein replay <task_id> --extra-context "hint: use the Foo class"` adds hint
4. Diff: compare replay result with original
5. Useful for debugging why an agent failed
"""),
    ("W7-10", "notification-channels", "backend", 2, "medium", "medium", """
# W7-10 — Notification Channels (Slack, Email, Desktop)

Get notified when tasks complete, fail, or need approval.

## Acceptance Criteria
1. Channels: Slack webhook, email (SMTP), desktop notification (terminal-notifier/notify-send)
2. Config: `notifications: {slack: {webhook: "..."}, events: [task_complete, task_failed, approval_needed]}`
3. Each notification includes: task title, status, cost, link to logs
4. Quiet hours: `notifications: {quiet: {start: "22:00", end: "08:00"}}`
"""),
    ("W7-11", "cost-prediction-before-run", "backend", 1, "medium", "medium", """
# W7-11 — Cost Prediction Before Run

Estimate cost before starting. Show prediction in plan approval screen.

## Acceptance Criteria
1. Before execution: estimate cost per task based on model × complexity × avg tokens
2. Show in plan display: "Estimated total: $3.47 (±20%)"
3. Historical calibration: use past runs to improve prediction accuracy
4. Warn if predicted cost exceeds budget
5. `bernstein estimate "goal text"` for quick prediction without running
"""),
    ("W7-12", "vscode-extension-enhancements", "backend", 2, "medium", "medium", """
# W7-12 — VS Code Extension: Live Dashboard

Embed Bernstein dashboard in VS Code sidebar. Show task status, agent logs, cost in real time.

## Acceptance Criteria
1. Webview panel showing task list with status badges
2. Click task to see agent log output
3. Cost tracker in status bar
4. "Run Bernstein" command in command palette
5. "Approve Task" action from notification
"""),
    ("W7-13", "jetbrains-plugin", "backend", 3, "large", "high", """
# W7-13 — JetBrains Plugin

Bernstein integration for IntelliJ IDEA, PyCharm, WebStorm.

## Acceptance Criteria
1. Tool window showing task dashboard
2. Run configuration for `bernstein run`
3. Agent log output in Run tab
4. Cost tracker in status bar
5. Diff view for agent changes
"""),
    ("W7-14", "guided-goal-decomposition", "backend", 2, "medium", "medium", """
# W7-14 — Guided Goal Decomposition

Interactive goal breakdown wizard. Help users write better goals.

## Acceptance Criteria
1. `bernstein plan --guided` enters interactive mode
2. Ask clarifying questions: "What should the tests cover?", "Which files to modify?"
3. Generate task list from answers
4. Review and edit before execution
5. Save as template for future use
"""),
    ("W7-15", "auto-pr-description", "backend", 2, "small", "low", """
# W7-15 — Auto-Generate PR Description

When agent work is done, auto-generate a PR description from task + changes.

## Acceptance Criteria
1. After merge: `bernstein pr --create` generates PR with description
2. PR body: task title, what changed (summary), test results, cost
3. Conventional commit title based on task type
4. Labels auto-applied based on changed files
"""),

    # ── W8: Testing & Evaluation (15) ──
    ("W8-01", "eval-framework-agent-quality", "qa", 1, "large", "high", """
# W8-01 — Eval Framework for Agent Output Quality

Systematic evaluation of agent code quality. Benchmark across models and tasks.

## Acceptance Criteria
1. `bernstein eval run` executes benchmark suite
2. Metrics: test pass rate, code correctness, style score, security score
3. Compare across models: "opus scores 92%, sonnet 78% on this benchmark"
4. Persistent results in `.sdd/eval/results/`
5. Regression detection: alert if quality drops between versions
"""),
    ("W8-02", "mutation-testing-quality-gate", "qa", 2, "medium", "medium", """
# W8-02 — Mutation Testing as Quality Gate

Verify that agent-written tests actually catch bugs (not just pass).

## Acceptance Criteria
1. After agent writes tests: run mutation testing (mutmut or cosmic-ray)
2. Mutation score threshold: >60% (configurable)
3. If score below threshold: task rejected, agent re-tries
4. `bernstein test --mutations` for manual check
"""),
    ("W8-03", "fuzz-testing-agent-outputs", "qa", 2, "medium", "medium", """
# W8-03 — Fuzz Testing Agent Outputs

Automatically fuzz test code written by agents to find edge cases.

## Acceptance Criteria
1. After agent task completes: run Hypothesis-based fuzz tests on new functions
2. Auto-generate fuzz targets from function signatures
3. Report crashes and edge cases as follow-up tasks
4. Configurable: `quality_gates: {fuzz: {enabled: true, duration: 30s}}`
"""),
    ("W8-04", "regression-test-suite", "qa", 1, "medium", "medium", """
# W8-04 — Regression Test Suite for Orchestrator

Comprehensive regression tests for the orchestrator itself (not agent code).

## Acceptance Criteria
1. Tests for: task lifecycle, routing, merge, cleanup, cost tracking
2. Property-based tests for scheduler fairness
3. Chaos tests: agent crash mid-task, server restart, network partition
4. Run in CI, <5min total execution time
5. 80%+ branch coverage on core/ modules
"""),
    ("W8-05", "swe-bench-integration", "qa", 2, "large", "high", """
# W8-05 — SWE-Bench Integration

Run SWE-Bench (real GitHub issues) through Bernstein to measure end-to-end quality.

## Acceptance Criteria
1. `bernstein eval swe-bench --subset lite` runs SWE-Bench Lite
2. Per-model results: resolve rate, cost, time
3. Compare: Bernstein orchestration vs single-agent
4. Publish results to leaderboard
"""),
    ("W8-06", "code-review-quality-gate", "qa", 1, "medium", "medium", """
# W8-06 — AI Code Review Quality Gate

Auto-review agent output with a second AI before merge.

## Acceptance Criteria
1. After agent completes: send diff to review agent (different model)
2. Review checks: correctness, style, security, test coverage
3. Review result: approve / request-changes / reject
4. If rejected: original agent gets feedback and retries
5. Configurable: `quality_gates: {ai_review: {enabled: true, model: opus}}`
"""),
    ("W8-07", "performance-benchmark-gate", "qa", 2, "medium", "medium", """
# W8-07 — Performance Benchmark Gate

Run performance benchmarks before and after agent changes. Block merge on regression.

## Acceptance Criteria
1. Config: `quality_gates: {benchmark: {command: "pytest benchmarks/", threshold: 10%}}`
2. Compare before/after: if >10% regression, block merge
3. Results logged for trend tracking
4. `bernstein benchmark` for manual check
"""),
    ("W8-08", "test-coverage-enforcement", "qa", 1, "small", "low", """
# W8-08 — Test Coverage Enforcement Gate

Require minimum test coverage for agent-modified files.

## Acceptance Criteria
1. Config: `quality_gates: {coverage: {min: 80%, new_code: 90%}}`
2. Run pytest-cov on changed files after agent completes
3. If below threshold: task rejected with "add more tests"
4. Dashboard shows coverage trend over time
"""),
    ("W8-09", "static-analysis-gate-semgrep", "qa", 2, "small", "low", """
# W8-09 — Static Analysis Gate (Semgrep)

Run Semgrep rules on agent output before merge.

## Acceptance Criteria
1. Run semgrep with project rules on changed files
2. Block merge on high-severity findings
3. Report findings as follow-up tasks
4. Config: `quality_gates: {semgrep: {rules: "p/python", severity: error}}`
"""),
    ("W8-10", "dependency-audit-gate", "qa", 2, "small", "low", """
# W8-10 — Dependency Audit Gate

Check for vulnerable dependencies when agent modifies requirements.

## Acceptance Criteria
1. If agent changes pyproject.toml/requirements.txt: run pip-audit
2. Block merge on known CVEs with severity >= high
3. Report as follow-up task if CVE found
4. Config: `quality_gates: {deps: {severity: high}}`
"""),
    ("W8-11", "llm-judge-evaluator", "qa", 2, "medium", "high", """
# W8-11 — LLM-as-Judge Evaluator

Use LLM to evaluate agent output quality on subjective criteria.

## Acceptance Criteria
1. Configurable rubric: "Is the code idiomatic?", "Are error messages helpful?"
2. Score 1-5 on each criterion
3. Results stored for model comparison
4. Use cheap model (haiku/flash) for judging
5. `bernstein eval judge <task_id>` for manual evaluation
"""),
    ("W8-12", "golden-test-suite", "qa", 2, "medium", "medium", """
# W8-12 — Golden Test Suite

Curated set of tasks with known-good solutions. Verify orchestrator doesn't regress.

## Acceptance Criteria
1. 20+ golden tasks in `tests/golden/`
2. Each: task description, expected files, expected test results
3. `bernstein eval golden` runs all, reports pass/fail
4. CI: run golden suite on every release
"""),
    ("W8-13", "chaos-engineering-agent-failures", "qa", 2, "medium", "high", """
# W8-13 — Chaos Engineering: Agent Failure Injection

Deliberately inject failures to verify resilience.

## Acceptance Criteria
1. `bernstein test --chaos` enables fault injection
2. Faults: agent crash, timeout, OOM, merge conflict, disk full
3. Verify: tasks retry, slots reclaimed, no data loss
4. Report: which faults were injected and how system recovered
"""),
    ("W8-14", "agent-output-diff-quality-score", "qa", 2, "small", "medium", """
# W8-14 — Agent Output Diff Quality Score

Score the quality of each agent's code changes.

## Acceptance Criteria
1. After each task: compute quality score from: test pass rate, lint errors, complexity delta, line count
2. Score 0-100, stored per task
3. Historical tracking: show agent quality trend
4. Route: prefer agents with higher quality scores for complex tasks
"""),
    ("W8-15", "continuous-eval-pipeline", "qa", 3, "large", "high", """
# W8-15 — Continuous Evaluation Pipeline

Run evals automatically on every Bernstein release. Track quality over time.

## Acceptance Criteria
1. CI job: on tag push, run golden suite + SWE-Bench-Lite + custom evals
2. Results published to `.sdd/eval/history/`
3. Dashboard: quality score over time, per model
4. Alert: if quality drops >5% from previous release
"""),

    # ── W9: Monitoring & Observability (10) ──
    ("W9-01", "opentelemetry-traces", "backend", 1, "medium", "high", """
# W9-01 — OpenTelemetry Trace Export

Export agent execution traces in OpenTelemetry format for Jaeger/Grafana/Datadog.

## Acceptance Criteria
1. Each task execution: one trace with spans for plan/spawn/execute/verify/merge
2. Export via OTLP (gRPC or HTTP)
3. Config: `telemetry: {otlp_endpoint: "http://localhost:4317"}`
4. Works with Jaeger, Grafana Tempo, Datadog (test with Jaeger)
5. Zero overhead when disabled
"""),
    ("W9-02", "prometheus-metrics-full", "backend", 2, "medium", "medium", """
# W9-02 — Full Prometheus Metrics

Comprehensive Prometheus metrics for all orchestrator operations.

## Acceptance Criteria
1. Metrics: tasks_total, tasks_active, agent_spawn_duration, merge_duration, cost_usd_total
2. Labels: model, adapter, role, status
3. `/metrics` endpoint in Prometheus text format
4. Grafana dashboard template in `docs/grafana/`
5. Alert rules for: high error rate, cost spike, agent stuck
"""),
    ("W9-03", "real-time-cost-streaming", "backend", 1, "medium", "medium", """
# W9-03 — Real-Time Cost Streaming

Stream cost updates to TUI and API as agents consume tokens.

## Acceptance Criteria
1. Cost updated every 10s (not just on task completion)
2. TUI shows live cost counter
3. SSE endpoint: `GET /events/cost` for real-time streaming
4. Per-agent cost breakdown in dashboard
"""),
    ("W9-04", "anomaly-detection-alerts", "backend", 2, "medium", "high", """
# W9-04 — Anomaly Detection for Agent Behavior

Detect unusual agent behavior: unexpected file modifications, excessive API calls, security violations.

## Acceptance Criteria
1. Baseline: learn normal agent behavior from past runs
2. Detect: files modified outside scope, excessive token usage (>3x average), suspicious patterns
3. Alert: log warning + optional Slack notification
4. Action: pause agent, await human review
"""),
    ("W9-05", "health-check-endpoint", "backend", 1, "small", "low", """
# W9-05 — Health Check Endpoints

Standard health check endpoints for load balancers and monitoring.

## Acceptance Criteria
1. `GET /health` → 200 if server up
2. `GET /health/ready` → 200 if server ready to accept tasks
3. `GET /health/live` → 200 if server process alive
4. Include: uptime, agent count, task queue depth
"""),
    ("W9-06", "distributed-tracing-cross-agent", "backend", 3, "large", "high", """
# W9-06 — Distributed Tracing Across Agents

Track a task from creation through multiple agent handoffs to completion.

## Acceptance Criteria
1. Trace ID propagated: manager → subtasks → agents → janitor → merge
2. Each step: timing, cost, model, result
3. Visualize in Jaeger/Zipkin as a single trace
4. `bernstein trace <task_id>` shows full trace in terminal
"""),
    ("W9-07", "error-budget-slo-tracking", "backend", 2, "medium", "medium", """
# W9-07 — Error Budget / SLO Tracking

Define SLOs (Service Level Objectives) and track error budget consumption.

## Acceptance Criteria
1. SLOs: task success rate ≥ 90%, merge success rate ≥ 95%, p95 task duration < 30min
2. Error budget: track remaining budget per SLO
3. When budget exhausted: reduce parallelism to preserve quality
4. Dashboard: SLO status, burn rate, time to budget exhaustion
"""),
    ("W9-08", "log-aggregation-elk-stack", "backend", 3, "medium", "medium", """
# W9-08 — Log Aggregation (ELK-Compatible)

Export structured logs in ELK-compatible format for centralized logging.

## Acceptance Criteria
1. JSON log format compatible with Elasticsearch ingest
2. Fields: timestamp, level, module, task_id, agent_id, message
3. Config: `logging: {format: json, output: file}`
4. Filebeat config template for ELK ingestion
"""),
    ("W9-09", "custom-dashboard-builder", "backend", 3, "medium", "medium", """
# W9-09 — Custom Dashboard Builder

Build custom monitoring dashboards from Bernstein metrics.

## Acceptance Criteria
1. Web dashboard at `bernstein dashboard --web`
2. Widgets: task board, cost chart, agent status, quality trend
3. Drag-and-drop layout (or predefined templates)
4. Auto-refresh with SSE
"""),
    ("W9-10", "incident-management-pagerduty", "backend", 3, "small", "medium", """
# W9-10 — Incident Management (PagerDuty/OpsGenie)

Integrate with incident management for critical failures.

## Acceptance Criteria
1. On critical failure: trigger PagerDuty/OpsGenie alert
2. Config: `incidents: {provider: pagerduty, key: "..."}`
3. Alert includes: error details, affected tasks, suggested action
4. Auto-resolve when issue fixed
"""),

    # ── W10: AI Engineering (15) ──
    ("W10-01", "prompt-versioning-ab-testing", "backend", 1, "medium", "high", """
# W10-01 — Prompt Versioning & A/B Testing

Version agent prompts and A/B test different versions.

## Acceptance Criteria
1. Prompts stored in `.sdd/prompts/` with version numbers
2. A/B test: split tasks between prompt v1 and v2
3. Metrics: success rate, quality score, cost per version
4. `bernstein prompts compare v1 v2` shows results
5. Auto-promote: winner becomes default after N tasks
"""),
    ("W10-02", "model-fallback-cascade-v2", "backend", 1, "medium", "medium", """
# W10-02 — Model Fallback Cascade v2

Intelligent fallback when primary model is rate-limited or down.

## Acceptance Criteria
1. Cascade: opus → sonnet → codex → gemini → qwen (configurable order)
2. Trigger: rate limit (429), timeout, API error
3. Automatic: no human intervention needed
4. Sticky: once cascaded, stay on fallback for 5 min (avoid ping-pong)
5. Metrics: cascade_count, fallback_model_usage
"""),
    ("W10-03", "token-budget-per-task", "backend", 1, "small", "medium", """
# W10-03 — Token Budget Per Task

Limit token usage per task to prevent runaway costs.

## Acceptance Criteria
1. Config: `budget: {max_tokens_per_task: {small: 10000, medium: 50000, large: 200000}}`
2. Agent prompt includes budget hint: "You have ~50K tokens for this task"
3. When budget approached: signal agent to wrap up
4. Hard limit: kill agent at 2x budget
"""),
    ("W10-04", "model-performance-benchmarks", "qa", 2, "medium", "medium", """
# W10-04 — Model Performance Benchmarks

Track model performance on Bernstein tasks over time.

## Acceptance Criteria
1. Record per-task: model, success, quality_score, cost, duration
2. Dashboard: model comparison table
3. Auto-update: after every session, metrics refreshed
4. `bernstein models compare` shows which model is best for what
"""),
    ("W10-05", "semantic-caching-v2", "backend", 2, "medium", "high", """
# W10-05 — Semantic Caching v2

Cache similar task results to avoid redundant API calls.

## Acceptance Criteria
1. If new task is semantically similar to completed task (>90% cosine similarity): reuse result
2. Cache stored in `.sdd/cache/`
3. TTL: cache expires after repo changes in relevant files
4. Metrics: cache_hit_rate, tokens_saved, cost_saved
5. `bernstein cache stats` shows cache effectiveness
"""),
    ("W10-06", "prompt-optimization-engine", "backend", 2, "large", "high", """
# W10-06 — Prompt Optimization Engine

Automatically improve agent prompts based on success/failure patterns.

## Acceptance Criteria
1. After N tasks: analyze which prompt patterns correlate with success
2. Suggest prompt modifications: "Adding 'write tests first' increased success by 15%"
3. Auto-apply low-risk optimizations (with human approval for high-risk)
4. Evolution loop integration: prompt optimization as a focus area
"""),
    ("W10-07", "chain-of-thought-extraction", "backend", 2, "medium", "medium", """
# W10-07 — Chain-of-Thought Extraction

Extract and store agent reasoning chains for debugging and improvement.

## Acceptance Criteria
1. Parse agent logs for reasoning patterns (plan, execute, verify)
2. Store structured CoT in `.sdd/traces/{task_id}/cot.json`
3. `bernstein trace <task_id> --cot` shows reasoning chain
4. Use CoT patterns to improve future prompts
"""),
    ("W10-08", "multi-modal-code-review", "backend", 3, "medium", "high", """
# W10-08 — Multi-Modal Code Review

Use vision models to review UI changes (screenshots before/after).

## Acceptance Criteria
1. For frontend tasks: capture screenshot before and after
2. Send to vision model for UI regression check
3. Report: "Button moved 10px left", "Color changed from #333 to #444"
4. Block merge on significant visual regressions
"""),
    ("W10-09", "auto-distillation-fine-tuning", "backend", 3, "large", "high", """
# W10-09 — Auto-Distillation of Successful Patterns

Fine-tune smaller models on successful agent outputs.

## Acceptance Criteria
1. Collect successful task → output pairs
2. Format as training data for smaller models
3. Fine-tune via OpenAI/Anthropic fine-tuning API
4. Route simple tasks to fine-tuned model (cheaper, faster)
5. Quality monitoring: ensure fine-tuned model maintains quality
"""),
    ("W10-10", "agent-collaboration-protocol", "backend", 2, "medium", "high", """
# W10-10 — Agent Collaboration Protocol

Allow agents to share intermediate results and ask each other questions.

## Acceptance Criteria
1. Agent A can post a question: "What API should I use for auth?"
2. Agent B (working on auth) can answer via shared bulletin board
3. Orchestrator routes questions to relevant agents
4. Async: agents don't block waiting for answers
5. Bulletin board: `.sdd/runtime/bulletin/`
"""),
    ("W10-11", "adaptive-parallelism", "backend", 1, "medium", "medium", """
# W10-11 — Adaptive Parallelism

Automatically adjust number of parallel agents based on system load and error rate.

## Acceptance Criteria
1. Start with configured max_workers
2. If error rate > 20%: reduce parallelism by 1
3. If error rate < 5% for 10 min: increase by 1 (up to max)
4. If CPU > 80%: pause spawning until load drops
5. Metrics: parallelism_level over time
"""),
    ("W10-12", "context-window-optimization", "backend", 2, "medium", "medium", """
# W10-12 — Context Window Optimization

Minimize wasted context tokens by smart prompt construction.

## Acceptance Criteria
1. Measure actual context usage per task
2. Compress: remove irrelevant files, truncate long files, summarize
3. Priority: task description > relevant code > project context > general instructions
4. Target: <50% context window utilization for small tasks
"""),
    ("W10-13", "model-routing-bandit-v2", "backend", 2, "medium", "high", """
# W10-13 — Model Routing Bandit v2

Improved epsilon-greedy bandit for model selection with contextual features.

## Acceptance Criteria
1. Features: task type, complexity, file types, repo language
2. Contextual bandit: Thompson sampling with feature vectors
3. Cold start: use prior from model pricing/capability data
4. Warm: learn from 50+ tasks
5. `bernstein routing explain <task_id>` shows why a model was chosen
"""),
    ("W10-14", "knowledge-graph-codebase", "backend", 3, "large", "high", """
# W10-14 — Codebase Knowledge Graph

Build a graph of code relationships for better task decomposition.

## Acceptance Criteria
1. Graph nodes: files, functions, classes, modules
2. Edges: imports, calls, inherits, references
3. Use for: impact analysis (what files affected by change?), scope estimation
4. Update incrementally on git changes
5. Query: `bernstein graph impact "auth.py"` shows affected files
"""),
    ("W10-15", "synthetic-data-test-gen", "qa", 3, "medium", "medium", """
# W10-15 — Synthetic Test Data Generation

Generate realistic test data for agent-written tests.

## Acceptance Criteria
1. Detect test fixtures that use hardcoded data
2. Generate realistic alternatives using Faker or LLM
3. Property-based test generation for pure functions
4. `bernstein test generate <module>` creates test file with synthetic data
"""),
]

# =============================================================================
# BRIEF FUTURE TICKETS (150) — F-series
# =============================================================================

BRIEF: list[tuple[str, str, str, int, str]] = []

# ── F1xx: Agent Intelligence (20) ──
agent_intel = [
    ("F101", "agent-self-reflection", "Agent self-reflection: agent reviews own output before submitting"),
    ("F102", "agent-peer-review", "Agent peer review: second agent reviews first agent's code"),
    ("F103", "agent-learning-from-failures", "Agent learning: extract lessons from failed tasks, inject into future prompts"),
    ("F104", "agent-specialization-profiles", "Agent specialization profiles: track what each model is best at"),
    ("F105", "agent-confidence-scoring", "Agent confidence scoring: agent rates own confidence in output (1-5)"),
    ("F106", "agent-task-decomposition", "Agent-driven task decomposition: agents propose subtasks"),
    ("F107", "agent-code-understanding", "Deep code understanding: agents build mental model of codebase"),
    ("F108", "agent-pair-programming", "Agent pair programming: two agents collaborate on one task"),
    ("F109", "agent-debate-mode", "Agent debate: two agents argue for different approaches, human picks"),
    ("F110", "agent-tool-use-expansion", "Agent tool expansion: agents use MCP tools beyond code editing"),
    ("F111", "agent-web-research", "Agent web research: agents search docs/Stack Overflow during task"),
    ("F112", "agent-design-doc-writing", "Agent design docs: write ADR/RFC before implementation"),
    ("F113", "agent-commit-message-quality", "Commit message quality: enforce conventional commits from agents"),
    ("F114", "agent-incremental-delivery", "Incremental delivery: agents commit work-in-progress checkpoints"),
    ("F115", "agent-priority-negotiation", "Priority negotiation: agents request priority changes based on findings"),
    ("F116", "agent-risk-assessment", "Agent risk assessment: agents flag risky changes before merge"),
    ("F117", "agent-documentation-gen", "Auto-documentation: agents generate/update docs for changed code"),
    ("F118", "agent-api-contract-checking", "API contract checking: verify API changes are backward compatible"),
    ("F119", "agent-dependency-awareness", "Dependency awareness: agents understand import graph when making changes"),
    ("F120", "agent-test-strategy-selection", "Test strategy selection: agent picks unit/integration/e2e based on change"),
]

# ── F2xx: Platform Features (20) ──
platform = [
    ("F201", "distributed-task-queue-redis", "Redis-backed distributed task queue for multi-machine deployment"),
    ("F202", "task-priorities-dynamic", "Dynamic task priority adjustment based on dependencies and deadlines"),
    ("F203", "task-tags-and-labels", "Task tagging/labeling system for filtering and organization"),
    ("F204", "task-time-estimation", "AI-based task time estimation from description and complexity"),
    ("F205", "parallel-merge-strategy", "Parallel merge strategy: merge non-conflicting branches simultaneously"),
    ("F206", "incremental-verification", "Incremental verification: only re-verify changed components"),
    ("F207", "hot-swap-agents", "Hot-swap agents: replace a stuck agent without losing progress"),
    ("F208", "task-queue-visualization", "Visual task queue: Kanban-style board in TUI"),
    ("F209", "api-versioning-v2", "API versioning: /v2/ endpoints with backward compat"),
    ("F210", "websocket-live-updates", "WebSocket live updates for web dashboard"),
    ("F211", "batch-mode-overnight-runs", "Batch mode: queue 50+ tasks for overnight execution"),
    ("F212", "agent-pool-management", "Agent pool management: warm pools of pre-authenticated agents"),
    ("F213", "task-dependencies-cross-session", "Cross-session task dependencies: task from session A blocks session B"),
    ("F214", "scheduled-maintenance-tasks", "Scheduled maintenance: auto-run dependency updates, linting, etc."),
    ("F215", "workspace-snapshots", "Workspace snapshots: save/restore .sdd state for reproducibility"),
    ("F216", "config-inheritance", "Config inheritance: base config + per-project overrides"),
    ("F217", "multi-language-support", "Multi-language support: agent prompts in user's language"),
    ("F218", "offline-queue-mode", "Offline queue: accept tasks while disconnected, execute on reconnect"),
    ("F219", "agent-routing-policies-yaml", "Routing policies in YAML: complex routing rules beyond simple model mapping"),
    ("F220", "task-templates-marketplace", "Task template marketplace: share/download community templates"),
]

# ── F3xx: Enterprise Scale (15) ──
enterprise = [
    ("F301", "multi-tenant-isolation", "Multi-tenant deployment: isolated workspaces per team"),
    ("F302", "horizontal-scaling-k8s", "Kubernetes horizontal scaling: auto-scale agent pods"),
    ("F303", "queue-based-architecture", "Queue-based architecture: RabbitMQ/SQS between orchestrator and agents"),
    ("F304", "high-availability-failover", "HA failover: standby orchestrator takes over on primary failure"),
    ("F305", "rate-limiting-per-tenant", "Per-tenant rate limiting and quota enforcement"),
    ("F306", "usage-metering-billing", "Usage metering: track and bill per-team agent usage"),
    ("F307", "admin-dashboard-web", "Web admin dashboard: manage teams, quotas, agents, configs"),
    ("F308", "ldap-active-directory", "LDAP/Active Directory integration for enterprise user management"),
    ("F309", "multi-region-deployment", "Multi-region deployment: agents in different geographic regions"),
    ("F310", "database-backed-state", "PostgreSQL-backed state: replace file-based .sdd/ for production"),
    ("F311", "agent-fleet-management", "Agent fleet management: centralized control of 100+ agents"),
    ("F312", "compliance-templates-industry", "Industry compliance templates: HIPAA, PCI-DSS, FedRAMP"),
    ("F313", "enterprise-sla-guarantees", "SLA guarantees: uptime, task completion, cost predictability"),
    ("F314", "white-label-branding", "White-label: custom branding for enterprise deployments"),
    ("F315", "on-premises-air-gapped", "Air-gapped deployment: fully on-premises with local models"),
]

# ── F4xx: Ecosystem & Integrations (20) ──
ecosystem = [
    ("F401", "github-actions-deep", "Deep GitHub Actions: trigger Bernstein from any GH event"),
    ("F402", "gitlab-ci-integration", "GitLab CI integration: trigger, report, merge from GitLab"),
    ("F403", "jira-bidirectional-sync", "Jira bidirectional sync: Jira issues ↔ Bernstein tasks"),
    ("F404", "linear-integration", "Linear integration: sync tasks with Linear project"),
    ("F405", "slack-bot-full", "Full Slack bot: create tasks, get status, approve via Slack"),
    ("F406", "discord-bot", "Discord bot: community agent management in Discord"),
    ("F407", "vs-code-marketplace-v2", "VS Code extension v2: inline suggestions, diagnostics, task creation"),
    ("F408", "terraform-provider", "Terraform provider: manage Bernstein infrastructure as code"),
    ("F409", "docker-compose-template", "Docker Compose template for one-command deployment"),
    ("F410", "helm-chart-k8s", "Helm chart for Kubernetes deployment"),
    ("F411", "github-marketplace-action", "GitHub Marketplace Action: use Bernstein as a GH Action"),
    ("F412", "mcp-server-hosting", "Host Bernstein as MCP server for other tools"),
    ("F413", "a2a-protocol-server", "Implement A2A protocol server for agent-to-agent communication"),
    ("F414", "datadog-integration", "Datadog integration: export metrics and traces"),
    ("F415", "sentry-error-tracking", "Sentry integration: report agent errors to Sentry"),
    ("F416", "notion-integration", "Notion integration: sync tasks with Notion databases"),
    ("F417", "google-cloud-build", "Google Cloud Build integration"),
    ("F418", "azure-devops-integration", "Azure DevOps integration: boards, repos, pipelines"),
    ("F419", "bitbucket-integration", "Bitbucket integration: trigger from BB webhooks"),
    ("F420", "vercel-deployment-hook", "Vercel deployment hook: trigger Bernstein on deploy events"),
]

# ── F5xx: Research & Novel (15) ──
research = [
    ("F501", "neuro-symbolic-reasoning", "Neuro-symbolic reasoning: combine LLM with formal verification"),
    ("F502", "program-synthesis-from-tests", "Program synthesis: generate code from test specifications"),
    ("F503", "formal-verification-integration", "Integrate formal verification (Dafny/TLA+) for critical code"),
    ("F504", "reinforcement-learning-routing", "RL-based routing: learn optimal model assignment from outcomes"),
    ("F505", "federated-learning-agents", "Federated learning: agents share learnings without sharing code"),
    ("F506", "self-improving-quality-gates", "Self-improving quality gates: gates adapt based on what they catch"),
    ("F507", "autonomous-architecture-evolution", "Autonomous architecture: agents propose and implement refactors"),
    ("F508", "code-generation-from-diagrams", "Code gen from diagrams: generate code from architecture diagrams"),
    ("F509", "natural-language-specification", "NL spec: generate formal specs from natural language requirements"),
    ("F510", "multi-objective-optimization", "Multi-objective optimization: balance cost, quality, speed simultaneously"),
    ("F511", "transfer-learning-across-repos", "Transfer learning: apply learnings from repo A to repo B"),
    ("F512", "causal-inference-debugging", "Causal inference: identify root cause of failures automatically"),
    ("F513", "emergent-behavior-detection", "Emergent behavior detection: identify unexpected agent coordination"),
    ("F514", "agent-swarm-intelligence", "Swarm intelligence: emergent task solving from simple agent rules"),
    ("F515", "quantum-ready-scheduling", "Quantum-ready scheduling: prepare for quantum optimization of task graphs"),
]

# ── F6xx: DevEx & Tooling (15) ──
devex = [
    ("F601", "cli-autocompletion", "Shell autocompletion for bash/zsh/fish"),
    ("F602", "man-pages", "Generate man pages from CLI help text"),
    ("F603", "config-migration-tool", "Config migration tool: upgrade bernstein.yaml between versions"),
    ("F604", "debug-mode-verbose", "Debug mode: `bernstein run --debug` with verbose logging"),
    ("F605", "dry-run-mode", "Dry run: `bernstein run --dry-run` shows what would happen"),
    ("F606", "export-import-tasks", "Export/import tasks: `bernstein export tasks.json`"),
    ("F607", "diff-viewer-terminal", "Terminal diff viewer: syntax-highlighted diffs in TUI"),
    ("F608", "agent-log-search", "Log search: `bernstein logs search 'error' --agent claude`"),
    ("F609", "configuration-wizard-tui", "TUI configuration wizard: interactive bernstein.yaml editor"),
    ("F610", "plugin-dev-toolkit", "Plugin development toolkit: scaffold, test, publish adapter plugins"),
    ("F611", "documentation-site-gen", "Auto-generate documentation site from code and docstrings"),
    ("F612", "changelog-from-tasks", "Generate changelog from completed tasks (not just git commits)"),
    ("F613", "api-playground", "API playground: interactive API explorer at /docs"),
    ("F614", "performance-profiler", "Built-in profiler: identify orchestrator bottlenecks"),
    ("F615", "error-message-improvement", "Error message audit: make every error actionable with fix suggestions"),
]

# ── F7xx: AI Safety (10) ──
safety = [
    ("F701", "output-content-filter", "Content filter: block agent output containing secrets, PII, or harmful code"),
    ("F702", "sandbox-escape-detection", "Sandbox escape detection: alert if agent tries to escape isolation"),
    ("F703", "privilege-escalation-guard", "Privilege escalation guard: agents can't sudo, modify system files"),
    ("F704", "supply-chain-attack-detection", "Supply chain attack: detect if agent introduces malicious dependencies"),
    ("F705", "code-injection-scanner", "Code injection scanner: detect SQL injection, XSS, SSRF in agent output"),
    ("F706", "agent-behavior-boundaries", "Behavior boundaries: define what agents can and cannot do per role"),
    ("F707", "human-in-the-loop-escalation", "HITL escalation: agent pauses and asks human when uncertain"),
    ("F708", "red-team-agent", "Red team agent: dedicated agent that tries to break other agents' code"),
    ("F709", "differential-privacy-logs", "Differential privacy: add noise to logs to prevent data extraction"),
    ("F710", "agent-hallucination-detection", "Hallucination detection: verify agent claims against codebase facts"),
]

# ── F8xx: Cost & Economics (10) ──
cost = [
    ("F801", "spot-instance-agents", "Spot instance agents: use preemptible compute for non-critical tasks"),
    ("F802", "cost-optimization-recommendations", "Cost optimization: suggest cheaper alternatives for expensive patterns"),
    ("F803", "token-waste-analysis", "Token waste analysis: identify prompts that waste tokens"),
    ("F804", "batch-api-usage", "Batch API usage: use Anthropic/OpenAI batch APIs for 50% cost reduction"),
    ("F805", "cost-sharing-teams", "Cost sharing: split costs across teams based on usage"),
    ("F806", "budget-forecasting", "Budget forecasting: predict monthly costs from current usage"),
    ("F807", "free-tier-maximizer", "Free tier maximizer: automatically use free tier quotas first"),
    ("F808", "cost-per-line-of-code", "Cost per line of code metric: track efficiency over time"),
    ("F809", "invoice-generation", "Invoice generation: generate cost reports for clients"),
    ("F810", "roi-calculator", "ROI calculator: compute developer hours saved vs agent costs"),
]

# ── F9xx: Community & Docs (10) ──
community = [
    ("F901", "contributor-guide", "Comprehensive contributor guide: setup, architecture, conventions"),
    ("F902", "example-projects-gallery", "Example projects gallery: real-world Bernstein use cases"),
    ("F903", "video-tutorial-series", "Video tutorial series: from install to production"),
    ("F904", "community-plugins-registry", "Community plugin registry: discover and install plugins"),
    ("F905", "benchmarks-leaderboard", "Public benchmarks leaderboard: compare Bernstein vs other tools"),
    ("F906", "architecture-decision-records", "ADR system: document all significant technical decisions"),
    ("F907", "api-reference-docs", "Auto-generated API reference documentation"),
    ("F908", "troubleshooting-guide", "Troubleshooting guide: common problems and solutions"),
    ("F909", "migration-guides", "Migration guides: from CrewAI, LangGraph, AutoGPT to Bernstein"),
    ("F910", "internalization-i18n", "i18n: translate CLI messages and docs to major languages"),
]

# ── F10xx: Far Future (15) ──
far_future = [
    ("F1001", "voice-controlled-orchestration", "Voice-controlled orchestration: 'Hey Bernstein, run tests'"),
    ("F1002", "ar-vr-code-visualization", "AR/VR code visualization: spatial view of agent work"),
    ("F1003", "brain-computer-interface", "BCI integration: thought-to-task (research prototype)"),
    ("F1004", "autonomous-ops-agent", "Autonomous ops agent: monitors prod, creates fix tasks on failure"),
    ("F1005", "self-replicating-agents", "Self-replicating agents: agents spawn sub-agents for subtasks"),
    ("F1006", "cross-language-orchestration", "Cross-language orchestration: Python + Rust + Go in one session"),
    ("F1007", "ai-project-manager", "AI project manager: LLM manages roadmap, prioritizes, assigns"),
    ("F1008", "code-archeology-agent", "Code archeology: agent understands why code was written (git blame + history)"),
    ("F1009", "predictive-bug-detection", "Predictive bug detection: identify likely bugs before they manifest"),
    ("F1010", "automated-refactoring-planner", "Automated refactoring planner: detect tech debt, plan remediation"),
    ("F1011", "natural-language-codebase-query", "NL codebase query: ask questions about any part of the code"),
    ("F1012", "agent-marketplace", "Agent marketplace: hire specialized agents from a marketplace"),
    ("F1013", "continuous-deployment-agent", "CD agent: agents deploy to staging, run smoke tests, promote to prod"),
    ("F1014", "design-system-agent", "Design system agent: maintain UI consistency across components"),
    ("F1015", "regulatory-compliance-agent", "Regulatory compliance agent: monitors changes for regulation violations"),
]

# Combine all brief tickets
for items in [agent_intel, platform, enterprise, ecosystem, research, devex, safety, cost, community, far_future]:
    for fid, slug, desc in items:
        BRIEF.append((fid, slug, desc, 3, "medium"))


def write_detailed(ticket_id: str, slug: str, role: str, priority: int,
                   scope: str, complexity: str, content: str) -> None:
    """Write a detailed ticket file."""
    path = BACKLOG / f"{ticket_id}-{slug}.md"
    text = textwrap.dedent(content).strip()
    # Add metadata header if not present
    if not text.startswith(f"# {ticket_id}"):
        lines = text.split("\n", 1)
        text = lines[0] + f"\n\n**Priority:** {priority}\n**Scope:** {scope}\n**Complexity:** {complexity}\n**Role:** {role}\n" + (lines[1] if len(lines) > 1 else "")
    else:
        # Insert metadata after first heading
        lines = text.split("\n", 1)
        text = lines[0] + f"\n\n**Priority:** {priority}\n**Scope:** {scope}\n**Complexity:** {complexity}\n**Role:** {role}\n" + (lines[1] if len(lines) > 1 else "")
    path.write_text(text + "\n", encoding="utf-8")


def write_brief(ticket_id: str, slug: str, description: str,
                priority: int, scope: str) -> None:
    """Write a brief future ticket."""
    path = BACKLOG / f"{ticket_id}-{slug}.md"
    text = f"""# {ticket_id} — {description.split(':')[0] if ':' in description else slug.replace('-', ' ').title()}

**Priority:** {priority}
**Scope:** {scope}

{description}
"""
    path.write_text(text, encoding="utf-8")


def main() -> None:
    count = 0

    # Write detailed tickets
    for ticket_id, slug, role, priority, scope, complexity, content in DETAILED:
        write_detailed(ticket_id, slug, role, priority, scope, complexity, content)
        count += 1

    # Write brief tickets
    for ticket_id, slug, description, priority, scope in BRIEF:
        write_brief(ticket_id, slug, description, priority, scope)
        count += 1

    print(f"Generated {count} tickets in {BACKLOG}")


if __name__ == "__main__":
    main()
