# Build tiered benchmark suite for evolution validation

**Role:** qa
**Priority:** 2 (normal)
**Scope:** large
**Complexity:** medium

## Problem
Self-evolution needs deterministic, reproducible evaluation. Research recommends
a "Golden Dataset" with 3 tiers:

1. Smoke tests (5-10): should ALWAYS pass, failure = critical regression
2. Capability tests (20-30): span intended features, establish baseline
3. Stretch tests (10-20): beyond current capability, progress = improvement

OpenAI cookbook recommends 3-6 binary tests per task (below 3: loopholes; above 6: gaming)

## Implementation
- Create tests/benchmarks/ directory structure:
  - tests/benchmarks/smoke/ — must always pass (safety invariants)
  - tests/benchmarks/capability/ — baseline capability
  - tests/benchmarks/stretch/ — aspirational targets
- Each benchmark is a YAML file with:
  - goal: task description
  - expected_signals: completion signals
  - max_cost_usd: budget cap
  - max_duration_seconds: timeout
- CLI: `bernstein benchmark run [--tier smoke|capability|stretch|all]`
- Results stored in .sdd/benchmarks/YYYY-MM-DD.jsonl
- Golden benchmarks NEVER exposed to evolution loop for optimization

## Files
- tests/benchmarks/smoke/ (new directory + YAML files)
- tests/benchmarks/capability/ (new directory + YAML files)
- src/bernstein/cli/main.py (add benchmark command)
- src/bernstein/evolution/benchmark.py (new — runner)

## Completion signals
- path_exists: tests/benchmarks/smoke/
- path_exists: src/bernstein/evolution/benchmark.py
- test_passes: uv run pytest tests/ -x -q
