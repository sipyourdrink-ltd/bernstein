# 624 — Bernstein Bench Suite

**Role:** backend
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** #607

## Problem

No standardized evaluation suite exists for multi-agent orchestration effectiveness. SWE-Bench evaluates individual agents, but nothing measures orchestration quality: how well tasks are decomposed, how efficiently agents are assigned, and how conflicts are resolved.

## Design

Create bernstein-bench, an evaluation suite for orchestration effectiveness. Define metrics: task completion rate, cost per completed task, merge conflict rate, CI pass rate on first attempt, wall-clock time vs sequential baseline, and context utilization efficiency. Curate a test corpus of 50+ tasks spanning different complexities and domains. Each task has a known-good solution for comparison. Build a runner that executes each task under different orchestration configurations (varying agent count, model mix, context strategy) and records all metrics. Produce a report card with scores and comparisons. The suite should be runnable by the community to benchmark their own configurations.

## Files to modify

- `benchmarks/bernstein-bench/runner.py` (new)
- `benchmarks/bernstein-bench/metrics.py` (new)
- `benchmarks/bernstein-bench/corpus/` (new — test tasks)
- `benchmarks/bernstein-bench/report.py` (new)
- `benchmarks/bernstein-bench/README.md` (new)

## Completion signal

- `python -m benchmarks.bernstein_bench` runs the full evaluation suite
- Report card generated with all defined metrics
- At least 50 test tasks in the corpus
