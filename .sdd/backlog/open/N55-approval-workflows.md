# N55 — Approval Workflows

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Critical tasks complete and take effect immediately with no human gate, which is unacceptable in regulated environments that require explicit approval before deployment or data changes.

## Solution
- Add `approval: required` field to task definitions in bernstein.yaml
- When a task with `approval: required` completes, pause execution and enter a "pending approval" state
- Support approval via interactive CLI prompt or webhook callback for async workflows
- Implement `bernstein approve <task-id>` to approve and continue execution
- Implement `bernstein reject <task-id>` to reject and abort downstream tasks

## Acceptance
- [ ] bernstein.yaml supports `approval: required` per task
- [ ] Tasks with approval required pause after completion and await approval
- [ ] `bernstein approve <task-id>` resumes execution
- [ ] `bernstein reject <task-id>` aborts downstream tasks
- [ ] Webhook callback endpoint accepts approval/rejection from external systems
- [ ] Approval events are recorded in the audit log
