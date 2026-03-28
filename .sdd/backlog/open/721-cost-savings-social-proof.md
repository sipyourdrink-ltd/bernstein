# 721 — Cost Savings Social Proof Dashboard

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Cost is the #1 developer pain point with AI agents. Bernstein already tracks costs and has budget caps, but the output of `bernstein cost` is just a table. We need a visually compelling, screenshot-worthy cost breakdown that users will share on Twitter. "Look how much Bernstein saved me vs running agents manually."

## Design

### Enhanced `bernstein cost` output
- Bar chart (ASCII) comparing: single-agent cost vs Bernstein cost
- Savings percentage highlighted in green
- Per-model breakdown with cost-per-task
- "You saved $X.XX (Y%) by using Bernstein's model cascade"

### Shareable summary
Generate a markdown snippet users can paste into PRs or tweets:
```
🎼 Bernstein run summary
   Tasks: 5 completed, 0 failed
   Time: 2m 34s (vs ~8m single agent)
   Cost: $0.42 (vs ~$1.20 single agent)
   Savings: $0.78 (65%)
```

## Files to modify

- `src/bernstein/cli/cost.py`
- `src/bernstein/core/cost_tracker.py`

## Completion signal

- `bernstein cost` shows visual savings comparison
- Shareable summary generated after each run
