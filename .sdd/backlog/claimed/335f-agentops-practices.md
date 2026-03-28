# 335f — AgentOps: DevOps Practices for Agent Systems

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Depends on:** none

## Problem

Traditional DevOps assumes human-written code. Agent-generated code at scale needs new practices: AgentOps. No orchestrator has codified these yet.

## Practices to implement

### 1. Agent SLOs (Service Level Objectives)
Define measurable targets:
- Task success rate: >90% (currently ~82%)
- P95 task completion time: <5 minutes
- Cost per task: <$0.50 average
- Zero secret leaks

Track in `.sdd/metrics/slos.json`. Alert when SLO is at risk.
Display in dashboard as traffic lights: green/yellow/red.

### 2. Error Budget (from SRE)
When success rate drops below SLO, automatically:
- Reduce max_agents (less parallel = fewer failures)
- Switch to more expensive but reliable models
- Increase human review gates
Resume normal operation when error budget recovers.

### 3. Runbook Automation
For common failure patterns, define automatic remediation:
```yaml
runbooks:
  import_error:
    detect: "ModuleNotFoundError"
    action: "pip install {module}"
  lint_failure:
    detect: "ruff check failed"
    action: "ruff check --fix"
  port_conflict:
    detect: "Address already in use"
    action: "kill process on port, retry"
```

### 4. Incident Response for Agents
When critical failures occur:
1. Auto-pause orchestration
2. Capture full state snapshot
3. Generate incident report (what failed, why, blast radius)
4. Notify via configured channels
5. Create post-mortem task for next run

### 5. Chaos Engineering for Agent Systems
Periodically inject failures to test resilience:
- Kill a random agent mid-task → does crash recovery work?
- Simulate rate limit → does fallback routing work?
- Remove a file being edited → does the agent handle it?

Run as `bernstein chaos --scenario rate-limit`.

### 6. Agent Canary Deployments
When Bernstein itself is updated:
- Run one agent on new code, rest on old
- Compare success rates
- Auto-rollback if new code is worse
- Leverages hot-reload (#331)

### 7. GitOps for Agent Config
Store all agent configuration (routing rules, quality gates, concurrency limits) in git. Changes to config automatically apply on next tick. No restart needed.

Already partially done with `.sdd/config/` — formalize as GitOps workflow where PR to config = change to live orchestrator.

## Files to modify

Implement as focused sub-tickets:
- SLOs: `src/bernstein/core/slo.py` (new)
- Error budget: integrate with orchestrator tick
- Runbooks: `src/bernstein/core/runbooks.py` (new)
- Incident response: enhance retrospective.py
- Chaos: `src/bernstein/cli/chaos_cmd.py` (new)
- Canary: enhance hot-reload with comparison
- GitOps: file watcher on .sdd/config/

## Completion signal

SLO dashboard visible in TUI and web dashboard.
Error budget auto-adjusts orchestrator behavior.
