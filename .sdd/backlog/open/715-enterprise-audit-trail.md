# 715 — Enterprise Audit Trail

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

Enterprise teams need to know: who approved what, what code was auto-generated, which model produced it, what it cost. This is a compliance requirement for SOC2, HIPAA, and financial services. No orchestrator in the ecosystem has this. First to market wins enterprise deals.

## Design

### Audit log format
Append-only JSONL log at `.sdd/audit/audit.jsonl`:
```json
{
  "timestamp": "2026-03-28T15:00:00Z",
  "event": "task.completed",
  "task_id": "T-001",
  "agent_id": "backend-abc123",
  "model": "claude-sonnet-4-6",
  "files_changed": ["src/auth.py", "tests/test_auth.py"],
  "tokens_used": 12400,
  "cost_usd": 0.12,
  "verified_by": "janitor",
  "approved_by": "auto",
  "git_commit": "abc123def"
}
```

### Events logged
- run.started, run.completed, run.failed
- task.created, task.claimed, task.completed, task.failed
- agent.spawned, agent.exited
- approval.requested, approval.granted, approval.denied
- budget.warning, budget.exceeded

### API endpoint
`GET /audit?since=2026-03-28&event=task.completed`

### Export
`bernstein audit export --format csv --since 2026-03-01`

## Files to modify

- `src/bernstein/core/audit.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/server.py`
- `tests/unit/test_audit.py` (new)

## Completion signal

- Audit log written for all events during a run
- API endpoint returns filtered audit entries
- CSV export works
