# Bernstein Benchmark Harnesses

This directory contains two different things, and they should not be confused:

1. `benchmarks/run_benchmark.py` is a modeling harness for internal workflow exploration.
2. `benchmarks/swe_bench/run.py eval` is the verified evaluation harness for public benchmark publication.

## Publication policy

- Public benchmark claims must come from `benchmarks/swe_bench/run.py eval`.
- Public v1 publication scope is Bernstein vs real single-agent baselines on SWE-Bench Lite.
- Simulation and modeling outputs in this directory are useful for previewing methodology, capacity planning, and CI smoke tests, but they are not public leaderboard evidence.
- CrewAI and LangGraph remain qualitative comparison targets until Bernstein can reproduce them under a Bernstein-owned live harness.

## Available harnesses

### Task-DAG modeling harness

`benchmarks/run_benchmark.py`

Purpose:
- Explore how dependency structure affects multi-agent speedup.
- Estimate cost and quality tradeoffs for Bernstein scheduling decisions.
- Smoke-test report generation without Docker or model credentials.

What it is not:
- Not a publishable benchmark.
- Not a SWE-Bench evaluation.
- Not a controlled cross-framework comparison.

### Issues modeling harness

`benchmarks/run_benchmark.py --issues-file benchmarks/issues.json`

Purpose:
- Run the same modeling approach against curated issue descriptions.
- Preview issue-shape coverage and generate internal markdown/JSON artifacts.

What it is not:
- Not live agent execution against SWE-Bench.
- Not acceptable for public benchmark claims.

### Verified SWE-Bench evaluation harness

`benchmarks/swe_bench/run.py eval`

Purpose:
- Run Bernstein and solo baselines against SWE-Bench Lite.
- Persist provenance-aware summary artifacts with `verified`, `source_type`, `dataset`, `sample_size`, `run_at`, and `commit_sha`.
- Generate public-safe benchmark reports that refuse to publish headline claims from mock or legacy artifacts.

Public v1 scenarios:
- `solo-sonnet`
- `solo-opus`
- `bernstein-sonnet`
- `bernstein-mixed`

## Reproducing

```bash
# Modeling harnesses (preview only)
uv run python benchmarks/run_benchmark.py
uv run python benchmarks/run_benchmark.py --issues-file benchmarks/issues.json

# Verified evaluation harness for public benchmark publication
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

# Generate public-safe markdown and docs outputs from saved artifacts
uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py
```

## Current checked-in artifacts

The checked-in SWE-Bench summaries under `benchmarks/swe_bench/results/` are marked as mock preview artifacts unless explicitly labeled otherwise in their provenance metadata. The public docs page at `docs/leaderboard.html` is generated from those artifacts and will stay in "verification in progress" mode until a verified eval run is checked in.
