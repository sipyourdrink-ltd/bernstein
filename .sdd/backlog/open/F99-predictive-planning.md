# F99 — Predictive Project Planning

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Project managers cannot accurately estimate cost, duration, or agent allocation for new projects, leading to overruns and inefficient resource usage.

## Solution
- Build a predictive planning module: given a requirements document, an ML model estimates total project cost, time, and optimal agent allocation
- Train the model on historical task data: task descriptions, actual durations, agent types used, costs incurred
- Input: requirements document (Markdown or plain text) parsed into task-like segments
- Output: estimated total hours, cost range (p25-p75), recommended agent team composition, suggested parallelism level
- Use a lightweight model (gradient-boosted trees) trained locally on user's own historical data
- Add `bernstein plan estimate <requirements.md>` CLI command
- Display confidence intervals and flag requirements with high uncertainty

## Acceptance
- [ ] ML model trained on historical bernstein task data (duration, cost, agents)
- [ ] Accepts requirements document as input and segments into estimated tasks
- [ ] Outputs total hours estimate, cost range (p25-p75), and agent allocation
- [ ] Recommends parallelism level based on task dependency analysis
- [ ] `bernstein plan estimate <file>` CLI command functional
- [ ] Confidence intervals displayed; high-uncertainty items flagged
- [ ] Model retrains incrementally as new task data accumulates
