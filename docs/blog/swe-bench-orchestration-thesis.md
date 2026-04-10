# The Bernstein SWE-Bench Publication Thesis

The thesis is no longer "publish the prettiest leaderboard."

The thesis is:

- benchmark claims need provenance;
- public comparisons should be narrow before they are broad;
- simulation is useful, but it is not evidence.

## What Bernstein publishes now

Bernstein's public benchmark bar is explicit:

- source: `benchmarks/swe_bench/run.py eval`
- provenance: `verified=true`, `source_type=eval`, dataset, sample size, run time, commit SHA
- public v1 scope: Bernstein vs `solo-sonnet` and `solo-opus` on SWE-Bench Lite

If those conditions are not met, Bernstein renders methodology and publication status instead of a winner table.

## What stays out of public benchmark tables for now

- simulated SWE-Bench summaries
- internal task-DAG modeling results
- issue-suite modeling results
- estimated CrewAI, LangGraph, or Agent HQ numbers

Those artifacts are still useful for engineering work. They are just not eligible for public benchmark claims.

## The first acceptable public result

The first publishable milestone is a pilot, not a leaderboard:

- `Verified Pilot Results (n=50)`
- date shown
- commit SHA shown
- reproducibility instructions adjacent to the numbers

After that, Bernstein can move to a full 300-instance SWE-Bench Lite run.

## Reproducing the verified path

```bash
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py
```
