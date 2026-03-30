# D10 — End-of-Run Summary Card

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When a workflow run finishes, users see the last task's output but have no consolidated view of the entire run's outcome. They must mentally reconstruct overall success, total cost, and time spent.

## Solution
- At the end of every `bernstein run` execution, print a summary card using Rich's `Table` with a box style (e.g., `box.ROUNDED`)
- Summary card contents:
  - Tasks completed vs. total (e.g., "4/5 tasks completed")
  - Tasks failed count (if any, highlighted in red)
  - Total wall-clock time
  - Total cost across all agents
  - Estimated time saved (calculated as 2x the total task time, representing manual dev time)
  - Quality score: percentage of tests that passed across all tasks that ran tests
- Use color coding: green header if all tasks passed, yellow if some failed, red if majority failed
- Support `--quiet` flag to suppress the summary card
- Also write the summary data to `.sdd/runs/<run-id>/summary.json` for programmatic access

## Acceptance
- [ ] Every `bernstein run` prints a summary card after all tasks complete
- [ ] The summary card shows: tasks completed/total, failed count, total time, total cost, estimated time saved, quality score
- [ ] The card header is green when all tasks pass, yellow when some fail, red when most fail
- [ ] `bernstein run --quiet` suppresses the summary card
- [ ] Summary data is written to `.sdd/runs/<run-id>/summary.json`
- [ ] The summary card renders correctly in terminals 80 columns wide and wider
