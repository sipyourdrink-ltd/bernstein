# 638 — Scheduled Orchestration

**Role:** backend
**Priority:** 4 (low)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein can only be triggered manually via the CLI. There is no support for scheduled or recurring orchestration runs. Regular maintenance tasks — daily code quality sweeps, weekly dependency updates, nightly test suite runs — require manual invocation or external cron jobs.

## Design

Add support for scheduled and cron-based agent orchestration runs. Define schedules in `.sdd/config.toml` under `[schedules]` using standard cron syntax. Each schedule specifies: task description, budget, model preference, and notification settings. Implement a lightweight scheduler daemon (`bernstein scheduler start`) that runs in the background and triggers orchestration runs on schedule. The daemon uses the system's cron or a Python scheduler (APScheduler) — no heavy infrastructure. Support common presets: `@daily`, `@weekly`, `@monthly`. Each scheduled run is a normal orchestration run with full recording and auditing. Add `bernstein scheduler list` to show upcoming runs and `bernstein scheduler logs` to show past scheduled run results.

## Files to modify

- `src/bernstein/core/scheduler.py` (new)
- `src/bernstein/cli/scheduler.py` (new)
- `.sdd/config.toml`
- `tests/unit/test_scheduler.py` (new)

## Completion signal

- `bernstein scheduler start` runs as a background daemon
- Scheduled tasks trigger at configured times
- `bernstein scheduler list` shows upcoming scheduled runs
