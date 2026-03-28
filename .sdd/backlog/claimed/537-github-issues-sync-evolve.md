# 537 — Sync GitHub Issues with evolve state: keep issues current

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** #520

## Problem

GitHub Issues for the Bernstein repo are stale after 1 day. Issues describe
work as "not started" when it's actually been implemented by evolve. There is
no mechanism to keep issues in sync with the actual codebase state.

This is also the foundation for distributed --evolve: if issues reflect real
state, multiple instances can coordinate through them.

## Design

### Issue audit on evolve startup
- On `bernstein run --evolve`, scan all open GitHub issues
- For each issue, check if the described work is already done:
  - Parse issue title/description for file paths, function names
  - Check if those files/functions exist in current codebase
  - Compare against .sdd/backlog/closed/ for matching tickets
- Auto-close issues that are done, with comment explaining what was implemented
- Update issues that are partially done with progress comment

### Periodic sync
- Every N evolve cycles (configurable), re-audit open issues
- New evolve proposals create/update matching GitHub issues (#520)
- Issue labels reflect backlog status: `open`, `claimed`, `in-progress`, `done`

### Bidirectional sync
- GitHub Issue created manually -> becomes .sdd/backlog task
- .sdd/backlog task completed -> GitHub Issue updated/closed
- Conflict resolution: GitHub Issue is source of truth for external input,
  .sdd/backlog is source of truth for internal state

## Files to modify
- New: `src/bernstein/core/github.py` — GitHub API integration
- `src/bernstein/evolution/loop.py` — issue audit hook
- `bernstein.yaml` — github.sync config

## Completion signal
- `bernstein run --evolve` closes stale issues automatically
- New evolve proposals appear as GitHub issues
- Issue state matches codebase reality within 1 evolve cycle
