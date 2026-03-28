# 645 — Agent Config Versioning

**Role:** backend
**Priority:** 5 (low)
**Scope:** medium
**Depends on:** #611

## Problem

Changes to agent prompts, role definitions, and orchestration configuration are not versioned or tracked. When a prompt change degrades performance, there is no way to identify what changed or roll back. This makes configuration tuning risky and unscientific.

## Design

Implement prompt and agent configuration versioning with rollback capability. Every change to a role template, system prompt, or orchestration config creates a new version in `.sdd/config-versions/`. Each version includes: a hash, timestamp, diff from previous version, and author. Link execution recordings to the config version used, enabling performance comparison across versions. Provide CLI commands: `bernstein config history` (show version log), `bernstein config diff v1 v2` (compare versions), `bernstein config rollback v1` (revert to a previous version). Track performance metrics per config version: task completion rate, cost per task, and time per task. Automatically flag performance regressions when a new config version underperforms the previous one.

## Files to modify

- `src/bernstein/core/config_versioning.py` (new)
- `src/bernstein/cli/config.py` (new)
- `src/bernstein/core/orchestrator.py`
- `tests/unit/test_config_versioning.py` (new)

## Completion signal

- Config changes create versioned snapshots
- `bernstein config history` shows version log
- `bernstein config rollback` reverts to a previous version
