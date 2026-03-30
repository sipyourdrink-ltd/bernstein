# F100 — Autonomous Continuous Improvement

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Prompt engineering and routing configuration require manual tuning based on observed metrics, which is tedious and delays optimization of orchestration performance.

## Solution
- Build an autonomous improvement loop triggered by `bernstein --evolve` or scheduled via nightly cron
- Analyze recent metrics: task success rates, agent latency, cost per task, verification failure patterns
- Propose changes: prompt rewording for underperforming agents, routing weight adjustments, retry policy tuning
- Test proposed changes in a sandboxed environment using recent task replays
- Compare sandbox results against baseline metrics; auto-apply changes that improve metrics without regression
- Generate a changelog entry for each applied change with before/after metrics
- Add safety guardrails: maximum change magnitude per cycle, rollback if production metrics degrade within 1 hour
- `bernstein evolve status` shows pending proposals, applied changes, and rollback history

## Acceptance
- [ ] `bernstein --evolve` analyzes recent metrics and proposes improvements
- [ ] Proposed changes tested in sandbox using task replays
- [ ] Changes auto-applied only if sandbox metrics improve without regression
- [ ] Changelog generated for each applied change with before/after comparison
- [ ] Safety guardrails: max change magnitude per cycle, auto-rollback on degradation
- [ ] Nightly cron scheduling supported
- [ ] `bernstein evolve status` shows proposals, applied changes, and rollback history
