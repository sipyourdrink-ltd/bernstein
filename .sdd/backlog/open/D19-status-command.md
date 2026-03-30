# D19 — Status Command for Last Run Summary

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
After a run completes (or fails), users have no quick way to review the summary without re-running Bernstein. They have to scroll back through terminal output or dig through log files manually.

## Solution
- Implement `bernstein status` that reads from `.sdd/runs/latest/` and displays a formatted summary.
- Summary includes: goal, overall status (success/failed/partial), tasks completed vs total, total cost, duration, and any errors.
- Format output as a clean table or structured block, e.g.:
  ```
  Last Run: 2025-03-15 14:32
  Goal:     "Add authentication to API"
  Status:   Partial (3/5 tasks completed)
  Cost:     $0.047
  Duration: 2m 18s
  Errors:   Task #4 failed — model timeout
  ```
- If no runs exist in `.sdd/runs/`, display: "No runs found. Try `bernstein quickstart` to get started."
- Support `--json` flag for machine-readable output.

## Acceptance
- [ ] `bernstein status` displays the last run summary with all listed fields
- [ ] Output is correctly formatted and human-readable
- [ ] When no runs exist, the helpful suggestion message is shown
- [ ] `--json` flag outputs valid JSON with the same fields
- [ ] Status correctly reflects partial completions (some tasks passed, some failed)
