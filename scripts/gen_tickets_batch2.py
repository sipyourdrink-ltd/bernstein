#!/usr/bin/env python3
"""Generate 250 tickets (T001-T250) for Bernstein roadmap batch 2.

Optimized for parallel execution by Bernstein's own agents:
- No inter-ticket dependencies within a category (max parallelism)
- Cross-category dependencies explicit and minimal
- Each ticket self-contained with acceptance criteria
"""
from pathlib import Path

BACKLOG = Path(".sdd/backlog/open")
BACKLOG.mkdir(parents=True, exist_ok=True)

# Categories: each category's tickets can run fully in parallel
TICKETS: list[tuple[str, str, str, int, str, str, str]] = []

def t(tid: str, slug: str, title: str, pri: int, scope: str, role: str, body: str) -> None:
    TICKETS.append((tid, slug, title, pri, scope, role, body))

# ═══════════════════════════════════════════════════════════════
# T001-T030: Developer Experience & Onboarding (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T001","auto-detect-project-type","Auto-detect project type (Python/Node/Rust/Go) on init",1,"small","backend","Scan for pyproject.toml/package.json/Cargo.toml/go.mod → set defaults")
t("T002","smart-goal-suggestions","Suggest goals based on codebase analysis",2,"medium","backend","Scan TODOs, failing tests, lint errors → suggest as goals")
t("T003","onboarding-progress-bar","Show onboarding progress for new users",2,"small","backend","Track: installed agents, first run, first recipe → show completion %")
t("T004","error-messages-audit","Make every error message actionable with fix suggestion",1,"medium","backend","Audit all raise/print error → add 'Fix: ...' hint")
t("T005","shell-completions-zsh","Zsh/Bash/Fish shell autocompletion",2,"small","backend","Use click's built-in completion generation")
t("T006","bernstein-explain-command","'bernstein explain <concept>' for inline help",2,"small","backend","Explain: task, recipe, agent, worktree, quality gate")
t("T007","config-validator","Validate bernstein.yaml on startup with clear errors",1,"small","backend","Check: required fields, valid model names, valid roles")
t("T008","first-run-tutorial","Interactive tutorial on first run",2,"medium","backend","Walk through: create goal, see TUI, review output")
t("T009","project-templates-init","'bernstein init --template flask' with preset configs",2,"small","backend","Templates: flask, fastapi, django, cli, library")
t("T010","cli-output-json-mode","'--json' flag on all CLI commands for scripting",2,"medium","backend","Every command: add --json that outputs structured JSON")
t("T011","progress-notifications-desktop","Desktop notifications on task complete/fail",2,"small","backend","Use terminal-notifier (macOS) / notify-send (Linux)")
t("T012","session-summary-email","Email session summary after overnight run",3,"small","backend","SMTP config in bernstein.yaml, send recap on stop")
t("T013","dark-light-theme-tui","Light/dark theme toggle for TUI",3,"small","backend","CSS theme switch in Textual dashboard")
t("T014","keyboard-shortcuts-help","'?' key in TUI shows all keyboard shortcuts",2,"small","backend","Overlay panel with keybinding reference")
t("T015","agent-log-syntax-highlight","Syntax highlighting for code in agent logs",2,"small","backend","Use Rich Syntax for .py/.js/.ts blocks in log viewer")
t("T016","task-search-filter","Search and filter tasks in TUI by role/status/keyword",2,"small","backend","Text input + DataTable filter")
t("T017","cost-sparkline-tui","Mini cost trend sparkline in TUI header",2,"small","backend","Show last 10 cost data points as ▁▂▃▅▇ sparkline")
t("T018","agent-uptime-display","Show agent uptime/duration in TUI agent panel",2,"small","backend","Format: '12m 34s' next to agent name")
t("T019","bernstein-version-check","Check for new version on startup (non-blocking)",3,"small","backend","PyPI version check, show 'Update available: X.Y.Z'")
t("T020","manpage-generation","Generate man pages from CLI help text",3,"small","backend","click-man or custom generator → man bernstein")
t("T021","config-diff-on-change","Show what changed when config is modified",2,"small","backend","Before applying: show diff of old vs new config")
t("T022","bernstein-doctor-v2","Enhanced doctor with fix suggestions",1,"medium","backend","Check: agents, server, config, disk, ports, stale PIDs → suggest fixes")
t("T023","output-format-table","'bernstein status --format table|json|csv'",2,"small","backend","Tabulate output for CLI pipelines")
t("T024","task-timeline-view","Timeline view of task execution in TUI",2,"medium","backend","Gantt-like horizontal bars showing task durations")
t("T025","bernstein-recap-v2","Enhanced recap with diff stats and quality scores",2,"small","backend","Show: files changed, tests added, lint score, cost breakdown")
t("T026","agent-selection-rationale","Show WHY a specific agent/model was chosen for task",2,"small","backend","Log: 'Chose opus because task.complexity=high and role=security'")
t("T027","worktree-status-in-tui","Show worktree branch/status in TUI agent panel",2,"small","backend","Display: branch name, files changed, last commit")
t("T028","bernstein-diff-enhanced","Enhanced diff view with file-level summaries",2,"medium","backend","Show: per-file summary, total lines added/removed, risk indicators")
t("T029","interactive-task-creation","'bernstein add' with interactive prompts",2,"small","backend","Prompt: title, role, priority, description → create task")
t("T030","startup-time-optimization","Optimize startup time to <1s for simple commands",1,"medium","backend","Lazy imports, defer heavy modules until needed")

# ═══════════════════════════════════════════════════════════════
# T031-T060: Cost Optimization & Token Efficiency (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T031","prompt-token-analyzer","Analyze prompt token usage and suggest reductions",2,"medium","backend","Report: system prompt %, context %, user prompt % per task")
t("T032","context-window-utilization","Track context window utilization per agent",2,"small","backend","Metric: % of context window used per task, alert if >80%")
t("T033","duplicate-context-detection","Detect and remove duplicate context across agents",2,"medium","backend","Hash context chunks, skip if agent already has it")
t("T034","incremental-context-updates","Send only changed files as context, not full dump",1,"medium","backend","Track file hashes, send delta on re-spawn")
t("T035","model-cost-comparison-live","Live model cost comparison during execution",2,"small","backend","Show: 'This would cost $X with opus, $Y with sonnet'")
t("T036","batch-api-router","Route eligible tasks to batch API for 50% discount",1,"medium","backend","Detect: non-urgent tasks → queue for batch processing")
t("T037","cached-prompt-prefix","Cache common prompt prefix across agent spawns",1,"medium","backend","Use Anthropic prompt caching for system prompt + project context")
t("T038","token-waste-report","Post-session report on token waste",2,"small","backend","Identify: retries, loops, oversized contexts that wasted tokens")
t("T039","cost-alert-slack","Send cost alerts to Slack when thresholds hit",2,"small","backend","Webhook on: 50%, 80%, 100% budget consumed")
t("T040","free-tier-detection","Detect and prioritize free tier quotas",2,"small","backend","Check: Gemini free tier, Codex free tier → use first")
t("T041","cost-per-commit-metric","Track cost per commit for efficiency measurement",2,"small","backend","Metric: total_cost / commits → trend over time")
t("T042","model-downgrade-on-retry","Use cheaper model on retry (opus fail → sonnet retry)",2,"small","backend","If task fails with opus, retry with sonnet before giving up")
t("T043","context-compression-v2","Compress long files to summaries before injecting",2,"medium","backend","Summarize: keep function signatures, remove implementations")
t("T044","token-budget-display-tui","Show token budget usage in TUI per agent",2,"small","backend","Bar: [████░░░░] 45% of 50K token budget")
t("T045","cost-forecast-next-hour","Forecast cost for next hour based on current rate",2,"small","backend","Extrapolate: current $/min × remaining tasks")
t("T046","idle-agent-detection-v2","Detect idle agents consuming tokens without progress",1,"small","backend","If agent has not changed files in 5 min → flag as idle")
t("T047","prompt-dedup-across-agents","Deduplicate identical prompt sections across agents",2,"medium","backend","Hash prompt sections, reuse cached responses")
t("T048","cost-breakdown-by-phase","Cost breakdown by phase: planning vs coding vs testing",2,"small","backend","Tag each API call with phase → aggregate")
t("T049","token-efficiency-leaderboard","Compare token efficiency across models and tasks",2,"small","backend","Rank: tokens per useful line of code by model")
t("T050","budget-rollover","Unused budget from one session rolls to next",3,"small","backend","Track: budget - spent → carry forward")
t("T051","cost-anomaly-detection","Alert on unusual cost spikes per task",2,"small","backend","Baseline: avg cost per task type. Alert if >3x")
t("T052","model-selection-explain","Explain model selection decision to user",2,"small","backend","Log: 'Selected sonnet: task.scope=small, history_success=92%'")
t("T053","prompt-cache-hit-rate","Track and display prompt cache hit rate",2,"small","backend","Metric: cache_hits / total_prompts → show in recap")
t("T054","cost-cap-per-agent","Hard cost cap per agent session",1,"small","backend","Config: max_cost_per_agent: $1.00. Kill agent at limit")
t("T055","token-usage-histogram","Histogram of token usage by task complexity",2,"small","backend","Show: small tasks avg 5K tokens, medium 25K, large 100K")
t("T056","cost-savings-report","Report how much Bernstein saved vs manual coding",2,"small","backend","Calculate: estimated_manual_hours × hourly_rate - api_cost")
t("T057","multi-provider-cost-arbitrage","Route to cheapest provider for equivalent quality",2,"medium","backend","Compare: Anthropic vs OpenAI vs Google prices in real-time")
t("T058","batch-task-grouping","Group small tasks into single batch for efficiency",2,"medium","backend","Combine 3-5 small tasks into one agent session")
t("T059","cost-dashboard-export","Export cost data as CSV/JSON for finance",2,"small","backend","'bernstein cost export --format csv --period 2026-03'")
t("T060","token-budget-warnings","Warn agent when approaching token budget",1,"small","backend","Inject: 'You have ~5K tokens remaining. Wrap up.'")

# ═══════════════════════════════════════════════════════════════
# T061-T090: Quality Gates & Verification (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T061","lint-gate-per-file","Run lint only on changed files (not full codebase)",1,"small","qa","Track changed files → ruff check <changed files>")
t("T062","type-check-gate-incremental","Incremental type check on changed modules only",1,"small","qa","pyright --files <changed> instead of full project")
t("T063","test-impact-analysis","Run only tests affected by changes",1,"medium","qa","Map: changed file → dependent test files → run subset")
t("T064","security-scan-gate","Run bandit/semgrep on agent output before merge",1,"small","security","Config: quality_gates.security_scan: true")
t("T065","dependency-check-gate","Check for vulnerable deps when requirements change",1,"small","security","Run pip-audit on changed dependency files")
t("T066","api-compat-gate","Check API backward compatibility on interface changes",2,"medium","qa","Compare: before/after function signatures")
t("T067","coverage-delta-gate","Require coverage delta ≥ 0 (no coverage regression)",1,"small","qa","Compare: coverage before and after agent changes")
t("T068","doc-coverage-gate","Check that new public functions have docstrings",2,"small","qa","AST parse → find public defs without docstrings")
t("T069","complexity-gate","Block merge if cyclomatic complexity increases >20%",2,"small","qa","Measure: radon cc before/after → compare")
t("T070","dead-code-gate","Run vulture on changed files to detect dead code",2,"small","qa","vulture <changed files> --min-confidence 80")
t("T071","import-cycle-gate","Detect import cycles introduced by agent",2,"small","qa","Run: importlab or custom cycle detector on changed modules")
t("T072","merge-conflict-predictor","Predict merge conflicts before agent starts",2,"medium","backend","Pre-check: git merge-tree to detect conflicts early")
t("T073","quality-score-aggregator","Aggregate quality scores into single 0-100 score",2,"small","qa","Combine: lint + type + test + coverage + complexity → score")
t("T074","quality-trend-chart","Show quality score trend over sessions",2,"small","qa","Store: per-session quality scores → plot trend")
t("T075","flaky-test-detector","Detect and quarantine flaky tests",2,"medium","qa","Run test 3x → if inconsistent results → mark as flaky")
t("T076","test-generation-for-uncovered","Auto-generate tests for uncovered functions",2,"medium","qa","Identify uncovered functions → create test task for agent")
t("T077","review-checklist-gate","Configurable review checklist before merge",2,"small","qa","Checklist: naming, error handling, logging, tests → auto-check")
t("T078","performance-regression-gate","Block merge on performance regression >10%",2,"medium","qa","Benchmark: before/after → compare latency/throughput")
t("T079","schema-migration-gate","Validate DB schema migrations are reversible",2,"small","qa","Check: down migration exists for every up migration")
t("T080","git-message-format-gate","Enforce conventional commit message format",1,"small","qa","Regex: ^(feat|fix|chore|docs|test|refactor): .+")
t("T081","file-size-gate","Warn if agent creates files >500 lines",2,"small","qa","Heuristic: large files should be decomposed")
t("T082","gate-execution-parallelism","Run quality gates in parallel (not sequential)",1,"medium","backend","Use asyncio.gather for independent gates")
t("T083","gate-result-caching","Cache gate results for unchanged files",2,"small","backend","Hash file content → skip re-check if unchanged")
t("T084","custom-gate-plugins","Allow custom quality gates via plugin system",2,"medium","backend","Plugin interface: run(changed_files) → GateResult")
t("T085","gate-bypass-with-reason","Allow gate bypass with documented reason",2,"small","backend","'--skip-gate lint --reason urgent-hotfix' → logged in audit")
t("T086","ai-code-review-gate","LLM reviews agent output (writer ≠ reviewer)",1,"medium","qa","Route: if Claude wrote → Codex reviews, and vice versa")
t("T087","dependency-graph-gate","Check dependency graph for cycles/violations",2,"small","qa","Verify: no circular imports, no layer violations")
t("T088","readme-update-gate","Prompt agent to update README if public API changes",2,"small","qa","Detect: new CLI command or config option → remind to document")
t("T089","changelog-update-gate","Auto-add changelog entry for feature/fix commits",2,"small","qa","Parse: conventional commit → append to CHANGELOG.md")
t("T090","gate-dashboard","Show quality gate results in TUI with pass/fail badges",2,"small","backend","Table: Gate | Status | Duration | Details")

# ═══════════════════════════════════════════════════════════════
# T091-T120: Enterprise & Compliance (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T091","tenant-id-tagging","Tag all tasks and logs with tenant_id",2,"small","backend","Every task/log/metric: add tenant_id field")
t("T092","team-budget-quotas","Per-team budget quotas with enforcement",2,"medium","backend","Config: teams.backend.budget: $100/month")
t("T093","audit-log-export-csv","Export audit logs as CSV for compliance review",2,"small","security","'bernstein audit export --format csv --period Q1-2026'")
t("T094","data-retention-cleanup","Auto-purge data older than retention period",2,"medium","backend","Config: retention.logs: 30d, retention.tasks: 90d")
t("T095","encryption-at-rest","Encrypt .sdd/ state files at rest",2,"medium","security","AES-256 encryption for tasks.jsonl, audit.jsonl")
t("T096","ip-allowlist-middleware","Restrict API access to allowed IP ranges",2,"small","security","Config: network.allowed_ips: ['10.0.0.0/8']")
t("T097","session-token-expiry","JWT session tokens with configurable expiry",2,"small","security","Config: auth.token_expiry: 24h")
t("T098","agent-permission-matrix","Define per-agent file/command permissions",1,"medium","security","Matrix: agent.backend can edit src/, cannot edit .github/")
t("T099","compliance-dashboard","Dashboard showing compliance status",2,"medium","security","Checklist: SOC2/GDPR/HIPAA requirements with pass/fail")
t("T100","audit-tamper-detection","Detect tampering in audit log files",2,"medium","security","Hash chain verification on audit log read")
t("T101","gdpr-data-export","Export all data for GDPR data subject request",3,"medium","security","'bernstein gdpr export --user user@email.com'")
t("T102","pii-redaction-in-logs","Automatically redact PII from log output",1,"small","security","Regex: email, phone, SSN, credit card → [REDACTED]")
t("T103","role-hierarchy","Role hierarchy: admin > developer > viewer",2,"medium","security","Inherit permissions from parent role")
t("T104","api-rate-limiting-v2","Per-endpoint rate limiting with configurable limits",2,"small","security","Config: rate_limit.tasks: 100/min, rate_limit.auth: 10/min")
t("T105","webhook-signature-verify","Verify webhook signatures (HMAC-SHA256)",2,"small","security","Validate: X-Hub-Signature-256 header on incoming webhooks")
t("T106","secret-rotation-reminder","Remind to rotate secrets approaching expiry",2,"small","security","Check: secret age > 90d → warn in doctor output")
t("T107","compliance-report-pdf","Generate PDF compliance report for auditors",3,"medium","security","Include: audit logs, access records, policy versions")
t("T108","access-log-structured","Structured access log for every API request",2,"small","security","Log: timestamp, user, method, path, status, duration")
t("T109","agent-identity-token","Unique identity token per agent session",2,"small","security","JWT with: agent_id, role, scope, expiry")
t("T110","vulnerability-scan-deps","Scheduled vulnerability scan of dependencies",2,"small","security","Weekly: pip-audit + safety → create fix tasks")
t("T111","change-approval-workflow","Multi-stage approval for high-risk changes",2,"medium","backend","High-risk task → security review → human approval → merge")
t("T112","data-classification-labels","Label repos/tasks with sensitivity: public/internal/confidential",2,"small","security","Config: data_classification: confidential")
t("T113","secure-defaults","Ensure all config defaults are secure",1,"small","security","Audit: default passwords, open ports, permissive CORS")
t("T114","network-policy-enforcement","Enforce network policies for agent subprocess",2,"medium","security","Block: agents cannot access internet unless whitelisted")
t("T115","incident-response-runbook","Auto-generate incident response runbook",3,"medium","security","Template: what happened, impact, fix applied, prevention")
t("T116","soc2-evidence-collector","Auto-collect SOC2 evidence from system state",2,"medium","security","Gather: access logs, change records, test results → package")
t("T117","hipaa-phi-detection","Detect PHI in agent output (healthcare compliance)",3,"medium","security","Scan: patient names, MRNs, diagnosis codes → block merge")
t("T118","fedramp-boundary-check","Verify agent actions stay within FedRAMP boundary",3,"medium","security","No external network calls, no data exfiltration")
t("T119","compliance-attestation","Generate signed compliance attestation document",3,"small","security","Cryptographic proof of compliance state at point in time")
t("T120","audit-retention-policy","Configurable audit log retention with auto-archive",2,"small","security","After N days: compress and archive old audit logs")

# ═══════════════════════════════════════════════════════════════
# T121-T150: Observability & Monitoring (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T121","otel-span-per-task","OpenTelemetry span for each task lifecycle",1,"medium","backend","Span: task_create → claim → execute → verify → merge")
t("T122","otel-span-per-gate","OpenTelemetry span for each quality gate execution",2,"small","backend","Span: gate_name, duration, pass/fail")
t("T123","prometheus-task-metrics","Prometheus counters for task create/complete/fail",1,"small","backend","Counters: tasks_total{status=done|failed|open}")
t("T124","prometheus-agent-metrics","Prometheus gauges for active agent count by role",1,"small","backend","Gauge: agents_active{role=backend|qa|security}")
t("T125","prometheus-cost-metric","Prometheus counter for accumulated cost",1,"small","backend","Counter: cost_usd_total{model=opus|sonnet}")
t("T126","grafana-dashboard-template","Grafana dashboard JSON template",2,"medium","backend","Pre-built dashboard: tasks, agents, cost, quality gates")
t("T127","structured-log-format","JSON structured logging for all components",2,"medium","backend","Fields: timestamp, level, component, task_id, agent_id, message")
t("T128","log-correlation-ids","Correlation ID across task → agent → gate → merge",2,"small","backend","Propagate: trace_id through all log entries for a task")
t("T129","error-classification","Classify errors into categories with counts",2,"small","backend","Categories: spawn_failure, merge_conflict, timeout, rate_limit")
t("T130","alert-rules-config","Configurable alert rules in bernstein.yaml",2,"medium","backend","Config: alerts: [{metric: error_rate, threshold: 0.1, channel: slack}]")
t("T131","health-endpoint-v2","Enhanced /health with component-level status",1,"small","backend","Components: server, spawner, database, agents → individual status")
t("T132","readiness-probe","GET /ready for load balancer readiness check",1,"small","backend","Returns 200 only when server can accept new tasks")
t("T133","liveness-probe","GET /alive for container liveness check",1,"small","backend","Returns 200 if process is alive (no dependency checks)")
t("T134","metrics-retention","Auto-cleanup metrics older than retention period",2,"small","backend","Config: metrics.retention: 30d")
t("T135","dashboard-auto-refresh","Web dashboard auto-refresh via SSE",2,"medium","backend","Real-time updates without polling: Server-Sent Events")
t("T136","agent-resource-usage","Track CPU/memory usage per agent process",2,"medium","backend","psutil: sample every 30s → store in metrics")
t("T137","disk-usage-monitoring","Alert when .sdd/ directory exceeds size threshold",2,"small","backend","Check: du -sh .sdd/ → alert if > 1GB")
t("T138","queue-depth-metric","Track task queue depth over time",2,"small","backend","Metric: tasks_queued_total → trend analysis")
t("T139","p95-task-duration","Track p50/p95/p99 task completion duration",2,"small","backend","Percentile: per role, per model → identify slow spots")
t("T140","error-rate-slo","SLO: error rate < 10% with error budget tracking",2,"medium","backend","Track: remaining error budget, burn rate, time to exhaustion")
t("T141","cost-slo","SLO: cost per task < $X with budget tracking",2,"medium","backend","Track: actual vs target cost per task type")
t("T142","throughput-slo","SLO: tasks completed per hour > N",2,"medium","backend","Track: actual throughput vs target → alert on deviation")
t("T143","incident-auto-detect","Auto-detect incidents from metric anomalies",2,"medium","backend","Z-score: if error rate > 3σ → create incident")
t("T144","incident-timeline","Generate incident timeline from logs and metrics",2,"medium","backend","Reconstruct: what happened when, in chronological order")
t("T145","log-search-cli","'bernstein logs search' with full-text search",2,"small","backend","Grep-like search across all agent logs")
t("T146","metric-comparison","Compare metrics between sessions",2,"small","backend","'bernstein compare session-1 session-2' → delta view")
t("T147","uptime-report","Generate uptime report for SLA compliance",3,"small","backend","Track: server uptime, planned vs unplanned downtime")
t("T148","trace-visualization","Visualize task trace as waterfall chart in terminal",2,"medium","backend","ASCII waterfall: task → subtasks → gates → merge")
t("T149","metric-export-otlp","Export metrics via OTLP to any collector",2,"medium","backend","OpenTelemetry metrics exporter alongside traces")
t("T150","custom-metric-hooks","Plugin hooks for custom metrics collection",2,"small","backend","Hook: on_task_complete → emit custom metric")

# ═══════════════════════════════════════════════════════════════
# T151-T180: Agent Intelligence & Routing (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T151","agent-skill-profile","Build skill profile per agent from history",2,"medium","backend","Track: success rate by (agent, task_type, complexity)")
t("T152","task-difficulty-estimator","Estimate task difficulty from description",2,"medium","backend","Features: word count, code refs, complexity keywords → score")
t("T153","smart-role-assignment","Auto-assign role based on task content analysis",2,"medium","backend","NLP: detect 'test' → qa, 'security' → security, 'auth' → backend")
t("T154","agent-warmup-pool","Keep pre-authenticated agent sessions warm",2,"medium","backend","Pool: 2 warm agents ready to claim tasks instantly")
t("T155","task-priority-decay","Reduce priority of old unclaimed tasks",2,"small","backend","After 1h unclaimed: priority +=1 (lower), free up slots")
t("T156","dependency-aware-scheduling","Schedule tasks respecting dependency graph",1,"medium","backend","Topological sort → schedule only unblocked tasks")
t("T157","load-balanced-spawning","Distribute tasks evenly across agent types",2,"small","backend","Round-robin: don't send all tasks to Claude if Codex available")
t("T158","agent-affinity","Prefer same agent for related tasks",2,"small","backend","If agent A did task 1, prefer A for task 2 (same context)")
t("T159","speculative-execution","Start next likely task before current completes",3,"medium","backend","Predict: which task will be claimed next → pre-spawn agent")
t("T160","task-splitting-heuristic","Auto-split large tasks into subtasks",2,"medium","backend","Heuristic: if estimated_minutes > 60 → suggest decomposition")
t("T161","model-ab-testing","A/B test models on identical tasks",2,"medium","backend","Split: 50% opus, 50% sonnet → compare quality/cost")
t("T162","routing-explanation-log","Log why each routing decision was made",2,"small","backend","Audit: 'Task T1 → claude/opus: complexity=high, role=security'")
t("T163","cooldown-after-failure","Cooldown period for agent after failure",2,"small","backend","After fail: 5min cooldown before re-assigning to same agent")
t("T164","task-deadline-enforcement","Enforce task deadlines with escalation",2,"medium","backend","Config: deadline per task. On miss: escalate to human")
t("T165","context-budget-per-task","Token budget for context injection per task",2,"small","backend","Limit: max 10K tokens of context per small task")
t("T166","agent-feedback-loop","Agent reports back on task difficulty and blockers",2,"medium","backend","Parse: agent output for 'blocked by', 'needs clarification'")
t("T167","retry-with-different-strategy","On retry, use different approach/model",2,"medium","backend","Strategy: first try sonnet, retry with opus + more context")
t("T168","task-dedup-detection","Detect and merge duplicate tasks",2,"small","backend","Cosine similarity on task descriptions → merge if >0.9")
t("T169","priority-queue-fairness","Ensure fair scheduling across priorities",2,"small","backend","Prevent: all P1 tasks starving P2/P3 tasks")
t("T170","agent-capability-matching","Match task requirements to agent capabilities",2,"medium","backend","Task needs: web access → only agents with tool support")
t("T171","workload-prediction","Predict workload from backlog analysis",2,"medium","backend","Estimate: total cost, total time, agents needed")
t("T172","scheduling-dry-run","Show scheduling plan without executing",2,"small","backend","'bernstein plan --dry-run' shows: which task → which agent")
t("T173","adaptive-concurrency","Auto-adjust max concurrent agents based on load",1,"medium","backend","If CPU >80% → reduce. If idle → increase")
t("T174","task-timeout-per-complexity","Different timeout per task complexity",2,"small","backend","small=15min, medium=30min, large=60min, xl=120min")
t("T175","agent-heartbeat-v2","Agent reports progress via heartbeat messages",2,"medium","backend","Parse: agent log for file changes → update progress bar")
t("T176","smart-retry-limit","Dynamic retry limit based on failure type",2,"small","backend","Transient (rate limit) → retry 3x. Permanent (bug) → no retry")
t("T177","task-grouping-by-file","Group tasks touching same files for sequential execution",2,"medium","backend","Prevent: two agents editing same file simultaneously")
t("T178","context-inheritance","Child task inherits parent's context decisions",2,"medium","backend","If parent used auth.py, child gets auth.py in context")
t("T179","routing-feedback-reward","Update routing weights based on outcomes",2,"medium","backend","Success: increase weight. Failure: decrease weight")
t("T180","scheduling-visualization","Visualize scheduling decisions in TUI",2,"small","backend","Show: task → agent assignment with reasoning")

# ═══════════════════════════════════════════════════════════════
# T181-T210: Ecosystem & Integrations (30 tickets)
# ═══════════════════════════════════════════════════════════════
t("T181","github-issue-to-task","Convert GitHub issue to Bernstein task automatically",1,"medium","backend","Webhook: issue labeled 'bernstein' → create task")
t("T182","github-pr-auto-create","Auto-create PR from completed agent work",1,"medium","backend","After merge to main: create PR with description + diff summary")
t("T183","github-check-run","Report quality gate results as GitHub Check Run",1,"medium","backend","API: POST check_runs with gate results per commit")
t("T184","github-status-badge","Dynamic GitHub badge showing Bernstein status",2,"small","backend","Badge: tasks completed, cost, quality score")
t("T185","slack-slash-commands","'/bernstein status' Slack slash command",2,"medium","backend","Commands: /status, /cost, /run, /stop, /approve")
t("T186","slack-interactive-approve","Approve tasks via Slack button",2,"medium","backend","Interactive message: [Approve] [Reject] [View Diff]")
t("T187","discord-bot-basic","Discord bot for task notifications",3,"medium","backend","Post: task complete, approval needed, cost alert")
t("T188","linear-sync","Sync tasks with Linear project management",2,"medium","backend","Bidirectional: Linear issue ↔ Bernstein task")
t("T189","jira-sync-v2","Enhanced Jira sync with custom fields",2,"medium","backend","Map: Jira fields → Bernstein task fields")
t("T190","notion-integration","Sync tasks with Notion database",3,"medium","backend","Notion API: create/update pages from task data")
t("T191","webhook-outbound","Send webhooks on task lifecycle events",1,"small","backend","Config: webhooks: [{url: '...', events: [task.done]}]")
t("T192","webhook-inbound-generic","Accept generic webhooks to create tasks",1,"small","backend","POST /webhook → create task from payload")
t("T193","mcp-server-mode","Expose Bernstein as MCP server for other tools",1,"medium","backend","Tools: create_task, list_tasks, get_status → MCP protocol")
t("T194","vscode-webview-panel","VS Code extension: task dashboard in sidebar",2,"medium","backend","Webview: task list, agent status, cost tracker")
t("T195","vscode-status-bar","VS Code status bar: live cost + active agents",2,"small","backend","StatusBarItem: '$0.47 | 3 agents'")
t("T196","jetbrains-tool-window","JetBrains plugin: tool window with dashboard",3,"large","backend","Swing/Compose UI: task list, logs, cost")
t("T197","neovim-telescope","Neovim: Telescope picker for tasks",3,"small","backend","Lua plugin: telescope.extensions.bernstein")
t("T198","docker-compose-template","Docker Compose for one-command deployment",2,"small","backend","Services: server + spawner + optional redis")
t("T199","helm-chart-k8s","Helm chart for Kubernetes deployment",2,"medium","backend","Chart: server deployment + spawner daemonset")
t("T200","github-action-v2","GitHub Action: use Bernstein in CI/CD",1,"medium","backend","Action: create task, wait for completion, report result")
t("T201","terraform-provider","Terraform provider for Bernstein workspace",3,"medium","backend","Resources: bernstein_workspace, bernstein_config")
t("T202","gitlab-ci-integration","GitLab CI integration: trigger + report",2,"medium","backend","gitlab-ci.yml template with Bernstein steps")
t("T203","bitbucket-pipeline","Bitbucket Pipeline integration",3,"small","backend","bitbucket-pipelines.yml template")
t("T204","pagerduty-integration","PagerDuty alert on critical failures",2,"small","backend","API: create incident on server crash or budget exhaustion")
t("T205","datadog-integration","Export metrics to Datadog",2,"medium","backend","DogStatsD: counters, gauges, histograms")
t("T206","sentry-integration","Report errors to Sentry",2,"small","backend","sentry-sdk: capture exceptions in server + orchestrator")
t("T207","email-notifications","Email notifications for task events",2,"small","backend","SMTP config → send on: complete, fail, approval needed")
t("T208","rss-feed","RSS feed of completed tasks",3,"small","backend","XML feed at /feed.xml with recent task completions")
t("T209","api-docs-swagger","Interactive API docs at /docs (Swagger UI)",1,"small","backend","FastAPI auto-generates this — ensure it works")
t("T210","sdk-python-typed","Python SDK with typed models and async",2,"medium","backend","Package: bernstein-sdk on PyPI with full API coverage")

# ═══════════════════════════════════════════════════════════════
# T211-T250: Advanced Features & Research (40 tickets)
# ═══════════════════════════════════════════════════════════════
t("T211","recipe-parser","Parse recipe YAML and create task graph",1,"medium","backend","YAML → validate → create tasks with sprint dependencies")
t("T212","recipe-cost-estimator","Estimate recipe cost before execution",1,"small","backend","Sum: estimated_cost per task × model pricing")
t("T213","recipe-progress-tracker","Track recipe progress (sprints completed / total)",1,"small","backend","Show: Sprint 2/4 complete, 65% done, $12.30 spent")
t("T214","recipe-marketplace-schema","Define schema for shareable recipes",2,"small","backend","JSON schema for recipe YAML validation")
t("T215","recipe-import-from-url","'bernstein recipe install <url>' from GitHub",2,"small","backend","Download YAML from URL → validate → save to recipes/")
t("T216","recipe-dry-run","'bernstein cook --dry-run' shows plan without executing",1,"small","backend","Parse recipe → show task graph + cost estimate → exit")
t("T217","recipe-resume","Resume recipe from last completed sprint",2,"medium","backend","Track: which sprints completed → resume from next")
t("T218","recipe-parameterization","Recipe with parameters: 'bernstein cook --param db=postgres'",2,"medium","backend","Template variables in recipe YAML: {{db}}, {{framework}}")
t("T219","knowledge-graph-build","Build codebase knowledge graph (files, functions, deps)",2,"large","backend","AST parse → graph: nodes=files/functions, edges=imports/calls")
t("T220","knowledge-graph-query","Query knowledge graph: 'what depends on auth.py?'",2,"medium","backend","Graph traversal → list of affected files and functions")
t("T221","impact-analysis","Impact analysis: 'what breaks if I change this function?'",2,"medium","backend","Trace: function → callers → tests → dependents")
t("T222","codebase-health-score","Overall codebase health score (0-100)",2,"medium","qa","Combine: test coverage, lint score, complexity, dep freshness")
t("T223","tech-debt-tracker","Track and prioritize technical debt automatically",2,"medium","backend","Detect: TODO/FIXME, complex functions, outdated deps → score")
t("T224","architecture-drift-detector","Detect when code deviates from architecture spec",3,"medium","backend","Compare: actual imports/deps vs documented architecture")
t("T225","auto-adr-generation","Auto-generate Architecture Decision Records from changes",3,"medium","backend","Detect: significant structural change → create ADR draft")
t("T226","cross-session-learning","Learn from past sessions to improve future routing",2,"medium","backend","Store: what worked/failed → adjust routing weights")
t("T227","agent-collaboration-board","Shared bulletin board for inter-agent communication",2,"medium","backend","Post: 'I found that auth uses JWT. Heads up.' → visible to others")
t("T228","semantic-search-codebase","'bernstein search' with semantic code search",2,"medium","backend","Embedding-based search across codebase → relevant files")
t("T229","auto-documentation-gen","Auto-generate docs from codebase + docstrings",2,"medium","backend","mkdocs/sphinx → generate from Python docstrings")
t("T230","changelog-from-tasks","Generate changelog from completed tasks",2,"small","backend","Group: task titles by type → Features, Fixes, etc.")
t("T231","release-notes-gen","Auto-generate release notes from session",2,"small","backend","Summarize: what changed, what was fixed, what's new")
t("T232","benchmark-suite","Reproducible benchmark suite for Bernstein itself",2,"medium","qa","Measure: throughput, cost, quality across standard tasks")
t("T233","benchmark-compare","Compare benchmarks across versions",2,"small","qa","Show: v1.3.10 vs v1.3.13 → what improved/regressed")
t("T234","chaos-test-server-crash","Chaos test: kill server mid-task, verify recovery",2,"medium","qa","Test: supervisor restarts, agents resume, no data loss")
t("T235","chaos-test-agent-oom","Chaos test: agent OOM, verify cleanup",2,"medium","qa","Test: slot reclaimed, task requeued, worktree preserved")
t("T236","chaos-test-disk-full","Chaos test: disk full during merge",2,"medium","qa","Test: graceful error, no corruption, cleanup old data")
t("T237","migration-guide-crewai","Migration guide: from CrewAI to Bernstein",2,"small","backend","Step-by-step: map CrewAI concepts → Bernstein concepts")
t("T238","migration-guide-langraph","Migration guide: from LangGraph to Bernstein",2,"small","backend","Step-by-step: map LangGraph concepts → Bernstein concepts")
t("T239","example-project-todo-app","Example project: TODO app built by Bernstein",2,"medium","backend","Complete recipe: Flask TODO API + frontend + tests")
t("T240","example-project-cli-tool","Example project: CLI tool built by Bernstein",2,"medium","backend","Complete recipe: Click CLI + tests + PyPI publish")
t("T241","contributor-guide","Comprehensive contributor guide with setup instructions",2,"medium","backend","How to: fork, setup dev env, run tests, submit PR")
t("T242","architecture-docs","Architecture documentation with diagrams",2,"medium","backend","Components: server, spawner, orchestrator, adapters, gates")
t("T243","video-demo-recording","Automated terminal recording for demos",2,"small","backend","Script: asciinema/VHS recording of bernstein run")
t("T244","plugin-adapter-template","Template for creating adapter plugins",2,"small","backend","Cookiecutter/scaffold: new adapter with tests")
t("T245","plugin-gate-template","Template for creating quality gate plugins",2,"small","backend","Scaffold: gate plugin with test harness")
t("T246","performance-profiler","Built-in profiler for orchestrator bottlenecks",2,"medium","backend","py-spy or cProfile integration: 'bernstein run --profile'")
t("T247","memory-leak-detection","Detect memory leaks in long-running sessions",2,"medium","backend","Track: RSS over time → alert if monotonically increasing")
t("T248","graceful-upgrade","Zero-downtime upgrade of running Bernstein",3,"large","backend","Strategy: start new version → drain old → switch → verify")
t("T249","distributed-task-queue","Redis-backed task queue for multi-machine",3,"large","backend","Replace file-based queue with Redis for scale")
t("T250","saas-multi-tenant","Multi-tenant SaaS architecture for hosted Bernstein",3,"large","backend","Tenant isolation: separate queues, budgets, configs, auth")


def main() -> None:
    count = 0
    for tid, slug, title, pri, scope, role, body in TICKETS:
        path = BACKLOG / f"{tid}-{slug}.yaml"
        # Determine model/tags from role and priority
        model = "sonnet" if pri <= 1 else "auto"
        if role == "security": model = "opus"
        tags_map = {"backend": "platform", "qa": "quality", "security": "security"}
        tag = tags_map.get(role, "feature")

        content = f"""---
id: "{tid}"
title: "{title}"
status: open
type: feature
priority: {pri}
scope: {scope}
complexity: {"high" if scope == "large" else "medium" if scope == "medium" else "low"}
role: {role}
model: {model}
effort: normal
estimated_minutes: {90 if scope == "large" else 45 if scope == "medium" else 20}
depends_on: []
blocks: []
tags: ["{tag}"]
janitor_signals: []
context_files: []
affected_paths: []
max_tokens: null
require_review: {"true" if pri <= 1 or role == "security" else "false"}
require_human_approval: false
---

## Summary

{body}

## Objective & Definition of Done

- [ ] {title} implemented and working
- [ ] Unit tests pass
- [ ] Ruff lint clean

## Steps

1. Read relevant source files
2. Implement the feature
3. Add/update unit tests
4. Run `uv run ruff check src/` and `uv run pytest tests/unit/ -x -q`

## Agent Notes

<!-- Reserved for implementing agent -->
"""
        path.write_text(content, encoding="utf-8")
        count += 1
    print(f"Generated {count} tickets in {BACKLOG}")


if __name__ == "__main__":
    main()
