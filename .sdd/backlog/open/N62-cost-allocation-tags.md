# N62 — Cost Allocation Tags

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Organizations running multiple teams and projects through Bernstein cannot attribute costs to specific teams or projects, making internal chargeback and budget tracking impossible.

## Solution
- Add `tags: {team: backend, project: auth}` support to bernstein.yaml at the workflow and task level
- Propagate tags to all task records and cost entries throughout execution
- Implement `bernstein cost --group-by team` (or any tag key) for cost breakdown reporting
- Store tags in run manifests and cost records for downstream querying
- Support arbitrary key-value pairs with no fixed schema

## Acceptance
- [ ] bernstein.yaml supports `tags:` with arbitrary key-value pairs
- [ ] Tags propagate from workflow definition to all child task and cost records
- [ ] `bernstein cost --group-by <tag-key>` produces a grouped cost breakdown
- [ ] Tags are persisted in run manifests and cost records
- [ ] Multiple tag keys can be combined (e.g., `--group-by team,project`)
