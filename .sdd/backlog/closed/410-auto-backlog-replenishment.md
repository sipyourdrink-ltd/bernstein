# 410 — Auto-backlog replenishment when idle

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
After 13 evolution cycles the backlog is empty and the system spins idle. The evolve mode rotates through focus areas (new_features, test_coverage, code_quality, performance, documentation) but doesn't generate ambitious enough work. The manager agent needs to be smarter about identifying real improvement opportunities rather than trivial increments.

## Implementation
1. Enhance manager agent's analysis phase in evolve mode:
   - Run `ruff check` and use output to generate fix tasks
   - Run `uv run pytest --tb=short` and parse failures for fix tasks
   - Run coverage analysis and create tasks for uncovered critical paths
   - Diff against DESIGN.md to find unimplemented features (currently 3 missing)
   - Analyze git log for patterns: frequently modified files = instability signals
2. Add "opportunity detector" to evolution loop:
   - After metrics aggregation, score opportunities by impact (not just presence)
   - Prioritize opportunities that affect multiple subsystems
   - Avoid re-creating tasks that were already done (check .sdd/backlog/done/ and closed/)
3. Add diminishing-returns detection:
   - If 3 consecutive cycles produce no meaningful changes, escalate to deeper analysis
   - Deeper analysis: benchmark against similar projects, check for missing patterns
4. Minimum viable backlog: always maintain >= 3 open tasks when in evolve mode

## Files
- src/bernstein/evolution/loop.py — smarter opportunity detection
- src/bernstein/evolution/detector.py — impact scoring
- src/bernstein/core/orchestrator.py — minimum backlog enforcement
- tests/unit/test_backlog_replenishment.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_backlog_replenishment.py -x -q
- file_contains: src/bernstein/evolution/detector.py :: impact_score


---
**completed**: 2026-03-28 04:32:12
**task_id**: 4b80492d2651
**result**: Completed: Add auto backlog replenishment to orchestrator
