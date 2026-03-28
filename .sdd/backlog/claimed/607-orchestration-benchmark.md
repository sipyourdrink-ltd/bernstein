# 607 — Orchestration Benchmark

**Role:** docs
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #602

## Problem

There is no empirical evidence that Bernstein-orchestrated multi-agent coding outperforms single-agent coding. Without benchmarks, the value proposition is theoretical. Benchmark content is the fastest path to GitHub stars and developer trust.

## Design

Create a benchmark comparing Bernstein-orchestrated agents vs a single agent on real GitHub issues. Select 20-30 issues from popular open-source repos spanning: bug fixes, feature additions, refactoring, and test writing. Measure: wall-clock time, total cost, CI pass rate, merge conflict rate, and code quality (linter score delta). Run each issue with both single-agent and Bernstein multi-agent configurations. Publish results with full methodology, raw data, and reproduction instructions. Include statistical significance testing. Host results in a `/benchmarks` directory with a summary page suitable for README embedding.

## Files to modify

- `benchmarks/README.md` (new)
- `benchmarks/run_benchmark.py` (new)
- `benchmarks/issues.json` (new — curated issue list)
- `benchmarks/results/` (new — raw results)

## Completion signal

- Benchmark suite runs against at least 20 real GitHub issues
- Results published with methodology and raw data
- Statistical comparison shows where multi-agent wins/loses
