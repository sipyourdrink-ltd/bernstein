# 335e — Prefect-Inspired Orchestration Patterns

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Depends on:** none

## Patterns adapted from Prefect for agent orchestration

### 1. Task State Machine (Prefect: flow states)
Bernstein tasks jump between open/claimed/done/failed. Prefect has richer states: Pending → Scheduled → Running → Completed/Failed/Cancelled/Paused/Retrying. Add:
- `paused` state — agent hit a question, waiting for human input
- `retrying` state — distinct from failed, shows retry in progress
- `cancelling` state — graceful cancel, agent saving work

### 2. Result Caching (Prefect: @task(cache_key_fn=...))
When an agent runs `git status`, `ls`, `cat file.py` — cache the result for N seconds. Next agent asking for the same file gets cached response. Saves tokens on repeated tool calls.
Implementation: hash(command + cwd + mtime) → cached output in `.sdd/cache/`.

### 3. Concurrency Limits by Tag (Prefect: ConcurrencyLimits)
Instead of global max_agents, allow per-tag limits:
```yaml
concurrency:
  backend: 3      # max 3 backend agents
  qa: 2           # max 2 QA agents
  architect: 1    # only 1 architect at a time
  docs: 1
```
Prevents 5 backend agents starving QA.

### 4. Artifacts (Prefect: create_markdown_artifact())
After each task, the agent produces structured artifacts:
- Diff summary (markdown table of files changed)
- Test results (pass/fail/skip counts)
- Cost receipt (tokens, model, dollars)
Stored in `.sdd/artifacts/{task_id}/` and visible in dashboard.

### 5. Blocks (Prefect: reusable config)
Reusable configuration blocks for secrets, connections, notifications:
```yaml
blocks:
  slack:
    type: webhook
    url: ${SLACK_WEBHOOK_URL}
  github:
    type: github-app
    app_id: 12345
```
Referenced by name in tasks: `notify: slack`. No more hardcoding URLs.

### 6. Automations (Prefect: event-driven triggers)
Declarative event → action rules:
```yaml
automations:
  - trigger: task.failed
    action: notify.slack
    filter: { role: security }
  - trigger: budget.exceeded
    action: orchestrator.stop
  - trigger: agent.stalled
    action: agent.kill
```

### 7. Work Pools (Prefect: distributed workers)
Group agents into pools by capability:
- `claude-pool` — Claude Code agents (high reasoning)
- `codex-pool` — Codex agents (fast, cheap)
- `gemini-pool` — Gemini agents (free tier, long context)
Tasks route to pools, not individual agents. Pool manages scaling.

### 8. Deployment Schedules (Prefect: cron deployments)
Schedule recurring Bernstein runs:
```yaml
schedules:
  - cron: "0 2 * * *"    # nightly at 2am
    goal: "Run security scan and fix findings"
    budget: "$5"
  - cron: "0 9 * * 1"    # Monday 9am
    goal: "Update dependencies and fix deprecations"
    budget: "$3"
```

### 9. Flow of Flows (Prefect: subflows)
Orchestrate orchestrations — a meta-flow that runs multiple Bernstein sessions:
```yaml
meta_flow:
  - step: "Plan architecture"
    agents: 1
    model: opus
  - step: "Implement features"
    agents: 5
    model: sonnet
    depends_on: [plan]
  - step: "QA and security review"
    agents: 3
    model: opus
    depends_on: [implement]
```

### 10. Idempotent Tasks (Prefect: idempotency keys)
If an agent crashes and restarts, detect that the task was partially completed and resume from where it left off instead of restarting from scratch. Hash task inputs as idempotency key.

## Files to modify

These are 10 separate sub-tickets. Implement individually:
- State machine: `src/bernstein/core/models.py` (add states)
- Result caching: `src/bernstein/core/cache.py` (new)
- Concurrency limits: `src/bernstein/core/orchestrator.py`
- Artifacts: `src/bernstein/core/artifacts.py` (new)
- Blocks: `src/bernstein/core/blocks.py` (new)
- Automations: `src/bernstein/core/automations.py` (new)
- Work pools: `src/bernstein/core/pools.py` (new)
- Schedules: cron integration in CLI
- Meta-flows: orchestrator-of-orchestrators
- Idempotency: crash recovery enhancement

## Completion signal

Each pattern implemented individually with tests.
