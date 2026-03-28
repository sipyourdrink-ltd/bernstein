# 634 — Auto Model Downgrade

**Role:** backend
**Priority:** 4 (low)
**Scope:** medium
**Depends on:** #601

## Problem

When a run approaches its budget limit, the only option is a hard stop. There is no graceful degradation path. Abruptly stopping agents mid-task wastes the work already completed and leaves tasks in an inconsistent state.

## Design

Implement automatic model downgrading when budget thresholds approach. Define a degradation chain: opus -> sonnet -> haiku (or equivalent per provider). At configurable budget thresholds (e.g., 70%, 85%, 95%), the orchestrator signals remaining agents to switch to cheaper models. New task assignments use the downgraded model. Already-running agents complete their current step but switch models for subsequent steps. Track the quality impact of downgrades by comparing task completion rates at each model tier. Allow users to configure the degradation chain and thresholds in `.sdd/config.toml`. Provide a `--no-downgrade` flag for users who prefer hard stops over quality degradation.

## Files to modify

- `src/bernstein/core/model_downgrader.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/cost_tracker.py`
- `.sdd/config.toml`
- `tests/unit/test_model_downgrader.py` (new)

## Completion signal

- As budget depletes, agents automatically switch to cheaper models
- Degradation chain configurable per provider
- `--no-downgrade` flag forces hard stops instead
