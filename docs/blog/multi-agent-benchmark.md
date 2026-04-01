# Benchmarking Multi-Agent Orchestration Without Fake Confidence

Bernstein now separates benchmarking into two buckets:

1. modeling harnesses that help us reason about orchestration behavior; and
2. verified evaluation artifacts that are safe to publish as benchmark claims.

That distinction matters. A task-DAG simulator can be useful and still be the wrong thing to put behind a public leaderboard headline.

## What counts as a public benchmark for Bernstein

For public claims, the bar is now explicit:

- the run must come from `benchmarks/swe_bench/run.py eval`
- the saved summaries must be marked `verified=true`
- the artifact must record dataset, sample size, run time, commit SHA, and scenario metadata
- v1 public scope is Bernstein vs real single-agent baselines on SWE-Bench Lite

If an artifact is `mock`, legacy, or missing provenance, Bernstein treats it as preview data only. The docs page renders methodology and publication status instead of a winner table.

## What the modeling harnesses are still good for

`benchmarks/run_benchmark.py` is still valuable. It helps answer questions like:

- how much parallelism is available in a task DAG?
- when does coordination overhead erase the benefit of more agents?
- which workloads look like good candidates for model mixing?

Those are useful engineering questions. They are just not the same as a verified public benchmark.

## Why the public scope is narrow

Bernstein is starting with one defensible publication track:

- `solo-sonnet`
- `solo-opus`
- `bernstein-sonnet`
- `bernstein-mixed`

All on SWE-Bench Lite, all under one Bernstein-owned harness.

CrewAI and LangGraph remain in the docs as architecture context, not as public numeric benchmark rows. Until Bernstein can reproduce those systems under a live, documented harness, publishing "we beat X by Y%" is marketing theater.

## Publication roadmap

The first acceptable public result is a clearly labeled pilot:

- `Verified Pilot Results (n=50)`
- date shown
- commit SHA shown
- methodology and reproduction path shown next to the numbers

After that, the next step is a full 300-instance SWE-Bench Lite run. Only then does it make sense to talk about stronger public benchmark positioning.

## Reproducing the real path

```bash
# Modeling harnesses (preview only)
uv run python benchmarks/run_benchmark.py
uv run python benchmarks/run_benchmark.py --issues-file benchmarks/issues.json

# Verified evaluation harness for public benchmark publication
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

# Generate public-safe markdown and docs outputs
uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py
```

The immediate consequence is simple: fewer flashy claims, more provenance. That is the right trade for a tool that is trying to earn trust from engineering teams.
