# 701 — Benchmark: Single Agent vs Bernstein Multi-Agent

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** none

## Problem

No empirical proof Bernstein is better than running a single agent. "Multi-agent orchestration" is a claim, not evidence. The single best content for HN/Reddit/Twitter is a benchmark showing 3x speedup with cost savings. Aider got famous partly through SWE-bench benchmarks. We need our own.

## Design

Create a repeatable benchmark suite:

### Test cases (10 minimum)
Pick real-world tasks across a sample codebase:
1. Add REST endpoints (3 routes) + tests
2. Refactor module into clean architecture
3. Add auth middleware + tests + docs
4. Fix 5 linting violations
5. Add error handling to all endpoints
6. Write integration test suite
7. Add rate limiting + tests
8. Create OpenAPI spec from code
9. Add logging and monitoring hooks
10. Security audit + fixes

### Comparison
For each task, run:
- **Single agent** (Claude Code alone): measure wall-clock time, cost, test pass rate
- **Bernstein 3-agent**: same metrics
- **Bernstein 5-agent**: same metrics

### Output
- `benchmarks/results/` with raw data (JSON)
- `benchmarks/README.md` with summary table and charts
- Badge for main README: "3.2x faster than single agent"
- Blog-ready markdown with methodology

## Files to modify

- `benchmarks/run_benchmark.py` (new)
- `benchmarks/tasks/` (new — task definitions)
- `benchmarks/results/` (new)
- `benchmarks/README.md` (new)
- `README.md` (add benchmark badge)

## Completion signal

- Benchmark runs end-to-end on at least 10 tasks
- Results show clear multi-agent advantage on parallelizable work
- Summary publishable on README and as blog post
