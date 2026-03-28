# 641 — A/B Testing Models

**Role:** backend
**Priority:** 5 (low)
**Scope:** medium
**Depends on:** #622

## Problem

There is no systematic way to compare model performance on the same task type. 75% of production teams use multiple models but rely on anecdotal evidence for model selection. Without A/B testing, model routing decisions are based on intuition rather than data.

## Design

Implement A/B testing across models for the same task type. When a task type has multiple candidate models, randomly assign a percentage of tasks to each model and record outcomes. Track metrics per model per task type: completion rate, cost, time to completion, CI pass rate, and code quality score. Use statistical significance testing (chi-squared for completion rate, t-test for continuous metrics) to determine winners. After sufficient data, recommend the winning model for each task type. Store A/B test configurations in `.sdd/config.toml` under `[ab_tests]` and results in `.sdd/ab_results/`. Provide a CLI command (`bernstein ab results`) to view current test status and recommendations. Require minimum sample size before declaring a winner (configurable, default: 30 tasks per model).

## Files to modify

- `src/bernstein/core/ab_testing.py` (new)
- `src/bernstein/core/model_router.py`
- `src/bernstein/cli/ab.py` (new)
- `.sdd/config.toml`
- `tests/unit/test_ab_testing.py` (new)

## Completion signal

- Tasks randomly assigned to candidate models based on A/B test config
- `bernstein ab results` shows statistical comparison
- Winner recommendation after minimum sample size reached
