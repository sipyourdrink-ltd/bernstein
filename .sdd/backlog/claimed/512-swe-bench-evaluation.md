# 512 — SWE-Bench evaluation harness for Bernstein

**Role:** qa
**Priority:** 2
**Scope:** medium
**Complexity:** medium

## Problem
No objective way to measure Bernstein's real-world effectiveness. "1777 tests pass" measures code health, not agent quality. SWE-Bench (and SWE-Bench Lite/Verified) is the industry standard for evaluating coding agents. Running Bernstein on SWE-Bench gives: (a) comparable metrics vs competitors, (b) regression detection when we change prompts/routing, (c) marketing material.

## Implementation

### 1. SWE-Bench harness (`src/bernstein/benchmark/swe_bench.py`)
- Download SWE-Bench Lite dataset (300 tasks, manageable cost)
- For each instance: create isolated env, seed the bug as a goal, run Bernstein, check if patch resolves the issue
- Score: resolved rate, cost per resolution, time per resolution

### 2. Evaluation modes
- `bernstein benchmark swe-bench --lite` — run SWE-Bench Lite (300 instances)
- `bernstein benchmark swe-bench --sample 20` — random sample for quick eval
- `bernstein benchmark swe-bench --instance django__django-11905` — single instance

### 3. Metrics and reporting
- Per-instance: resolved/unresolved, cost, time, agent count, retries
- Aggregate: resolve rate, median cost, median time, cost-effectiveness ratio
- Compare across Bernstein configs (model, effort, evolve vs no-evolve)
- Export to `.sdd/benchmark/swe_bench_results.json`

### 4. Custom benchmark suite
In addition to SWE-Bench, define Bernstein-specific benchmarks:
- Multi-file refactoring tasks
- Feature implementation from spec
- Test coverage improvement
- Security audit tasks
These test Bernstein's multi-agent coordination, not just single-agent coding.

## Files
- src/bernstein/benchmark/swe_bench.py (new)
- src/bernstein/cli/main.py — add swe-bench subcommand
- tests/unit/test_swe_bench_harness.py (new)

## Completion signals
- file_contains: src/bernstein/benchmark/swe_bench.py :: SWEBenchRunner
