---
name: bernstein-cost
description: >
  Show detailed cost breakdown and budget status using Bernstein's cost reporting.
  Use when the user asks about spending, budget, cost per model, cost per agent,
  or wants a cost projection.
---

# Bernstein Cost Tracker

Show detailed cost analysis and budget status.

## When to Use

- User asks "how much have we spent?" or "what's the cost?"
- User wants per-model or per-agent cost breakdown
- User asks about budget remaining or cost projection
- User says "are we over budget?"

## Instructions

1. Run `scripts/costs.sh` to fetch cost data.
2. Run `scripts/costs.sh projection` for cost forecast.
3. Present a clear cost report:

```
## Cost Report

**Total spent:** $X.XX / $Y.YY budget (Z%)
**Status:** {OK | WARNING | OVER BUDGET}

### Per Model
| Model | Tasks | Cost | Avg/Task |
|-------|-------|------|----------|
| claude-sonnet-4 | 12 | $1.84 | $0.15 |
| gpt-4.1 | 5 | $0.62 | $0.12 |

### Per Agent
| Agent | Role | Tasks Done | Cost |
|-------|------|------------|------|
| claude-backend-01 | backend | 3 | $0.45 |

### Projection
At current rate, this run will cost ~$X.XX total.
Estimated completion: Y tasks remaining, ~Z minutes.
```

4. If budget warning is active, highlight it prominently.
5. If over budget, note that Bernstein has paused new task claims.
