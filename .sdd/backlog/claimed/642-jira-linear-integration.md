# 642 — Jira/Linear Integration

**Role:** backend
**Priority:** 5 (low)
**Scope:** medium
**Depends on:** none

## Problem

Teams using Jira or Linear must manually copy issues into Bernstein's backlog. There is no integration with external project management tools. ComposioHQ Agent Orchestra supports tracker integration, setting a competitive expectation.

## Design

Add Jira and Linear integration for bidirectional issue-to-task synchronization. Import: pull issues from Jira/Linear into Bernstein's backlog, mapping fields (title -> task name, description -> task spec, priority -> priority, labels -> role assignment). Export: when Bernstein completes a task, update the corresponding Jira/Linear issue with results (PR link, cost, duration). Use official APIs with API key authentication. Support selective sync: filter by project, label, or assignee. Implement a sync command (`bernstein sync jira`, `bernstein sync linear`) and a watch mode for continuous sync. Store integration credentials in environment variables, not config files. Respect rate limits and implement exponential backoff.

## Files to modify

- `src/bernstein/integrations/jira.py` (new)
- `src/bernstein/integrations/linear.py` (new)
- `src/bernstein/cli/sync.py` (new)
- `docs/integrations/jira.md` (new)
- `docs/integrations/linear.md` (new)
- `tests/unit/test_jira_integration.py` (new)

## Completion signal

- `bernstein sync jira` imports issues into Bernstein backlog
- Completed tasks update the source Jira/Linear issue
- Selective sync filters work (project, label, assignee)
