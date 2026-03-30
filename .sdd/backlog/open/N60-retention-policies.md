# N60 — Retention Policies

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
The `.sdd/runs/` and `.sdd/traces/` directories grow indefinitely, wasting disk space and complicating compliance with data retention requirements.

## Solution
- Add `retention:` section to bernstein.yaml with configurable TTLs for runs and traces
- Auto-purge `.sdd/runs/` entries older than N days
- Auto-purge `.sdd/traces/` entries older than M days
- Implement `bernstein gc` for manual garbage collection trigger
- Support cron-compatible schedule syntax for automated purging (e.g., `schedule: "0 3 * * 0"`)
- Log all purge operations to the audit trail

## Acceptance
- [ ] `retention:` section in bernstein.yaml accepts TTL values for runs and traces
- [ ] Automatic purging deletes runs older than configured threshold
- [ ] Automatic purging deletes traces older than configured threshold
- [ ] `bernstein gc` triggers manual garbage collection
- [ ] Cron-compatible schedule syntax is supported for automated runs
- [ ] All purge operations are recorded in the audit log
