---
name: bernstein-status
description: >
  Show Bernstein status for the orchestration run being inspected - active agents, task progress, costs, and alerts.
  Use when the user asks about Bernstein status, what agents are doing, task progress,
  how much has been spent, or what's happening with the build.
---

# Bernstein Status

Show Bernstein status for the current orchestration run.

## When to Use

- User asks "what's the status?" or "how's the build going?"
- User wants to see active agents, open tasks, or costs
- User asks about progress, failures, or alerts
- User wants a quick overview of the orchestration run

## Instructions

1. Run `scripts/status.sh` to fetch the full dashboard data from the Bernstein API.
2. Parse the JSON output and present a clear summary:

### Summary format

```
## Bernstein Status

**Agents:** {active_count} active | **Tasks:** {done}/{total} done | **Cost:** ${cost_usd}

### Active Agents
| Agent | Role | Model | Runtime | Task | Cost |
|-------|------|-------|---------|------|------|
| {id}  | {role} | {model} | {runtime}m | {task_title} | ${cost} |

### Tasks
- {open} open, {claimed} claimed, {done} done, {failed} failed

### Alerts
- {alert messages if any}

### Cost Breakdown
- Budget: ${budget} | Spent: ${spent} ({percentage}%)
- Per model: {breakdown}
```

3. If the API is not reachable, tell the user to start Bernstein with `bernstein run`.
4. Highlight any alerts (failed tasks, budget warnings, stalled agents) prominently.
