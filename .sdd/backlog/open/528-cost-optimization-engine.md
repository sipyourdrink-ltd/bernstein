# 528 — Intelligent cost optimization engine

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium

## Problem

Model routing exists (routing.yaml) but is static rules. With hundreds of agents
running, cost becomes critical. Need dynamic routing that learns which model/effort
gives best ROI per task type, and auto-optimizes spend.

Ruflo already does 3-tier routing (WebAssembly -> Haiku -> Opus) claiming 2.5x
usage extension. Bernstein should match or beat this.

## Design

### Dynamic model selection
- Track per-task-type metrics: success_rate, cost, latency by model
- Build a simple bandit (epsilon-greedy) that explores model choices
- After N observations, converge on cheapest model that meets quality threshold
- Quality threshold: task success rate >= 80% for that task type

### Cost budget enforcement
- `bernstein run --budget $50` — hard cap on total spend
- Real-time cost tracking per agent, per task, per cycle
- Alerts when approaching budget limit
- Graceful degradation: switch to cheaper models as budget depletes

### Model cascade pattern
- Try cheapest model first (Haiku/free tier)
- If task fails or quality is low, retry with better model (Sonnet)
- If still fails, escalate to Opus
- Track cascade depth as metric

### Cost reporting
- `bernstein cost` — shows spend breakdown by model, role, task type
- Daily/weekly cost trends
- Projected monthly cost at current rate
- "Savings vs all-Opus baseline" metric

## Files to modify
- `src/bernstein/core/router.py` — dynamic routing logic
- `src/bernstein/core/models.py` — cost tracking fields
- `src/bernstein/core/orchestrator.py` — budget enforcement
- New: `src/bernstein/core/cost.py` — bandit, cascade, reporting

## Completion signal
- Routing automatically shifts to cheaper models for simple tasks
- `bernstein cost` shows accurate spend breakdown
- Budget cap stops execution before overspend
