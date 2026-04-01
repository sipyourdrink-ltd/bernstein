# SWE-Bench Lite Benchmarks

> **Status:** Verified public benchmark results: in progress
> Current page shows methodology, harness coverage, and reproducibility path.
> Checked-in artifacts are treated as preview data until verified eval results are published.

## Current Artifact State

| Scenario | Source type | Verified | Sample size | Notes |
|---|---|---|---:|---|
| Solo Sonnet | mock | No | 300 | Checked-in mock preview artifact. Do not use for public benchmark claims. |
| Solo Opus | mock | No | 300 | Checked-in mock preview artifact. Do not use for public benchmark claims. |
| Bernstein 3x Sonnet | mock | No | 300 | Checked-in mock preview artifact. Do not use for public benchmark claims. |
| Bernstein Mixed | mock | No | 300 | Checked-in mock preview artifact. Do not use for public benchmark claims. |

## Publication Blockers

- At least one required scenario is not marked as a verified `eval` artifact.

## Public Benchmark Policy

- Only `benchmarks/swe_bench/run.py eval` artifacts marked `verified=true` are eligible for public benchmark claims.
- Public v1 comparisons are limited to Bernstein vs real single-agent baselines on SWE-Bench Lite.
- Competitor framework content stays qualitative until Bernstein can reproduce those systems with a Bernstein-owned live harness.

## Harness Coverage

| Scenario | Purpose |
|---|---|
| Solo Sonnet | Cheap single-agent baseline |
| Solo Opus | Expensive single-agent baseline |
| Bernstein 3x Sonnet | All-Sonnet Bernstein pipeline |
| Bernstein Mixed | Cost-optimized Bernstein pipeline |

## Reproducing

```bash
# Simulation/modeling harnesses (preview only, not public benchmark claims)
uv run python benchmarks/run_benchmark.py
uv run python benchmarks/run_benchmark.py --issues-file benchmarks/issues.json

# Verified evaluation harness for public benchmark publication
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

# Generate benchmark markdown and the docs page from saved artifacts
uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py
```
