# SWE-Bench Lite Evaluation: Bernstein Scaffolding Thesis

> **NOTE:** These results are **simulated** (generated with `run.py mock`).
> Replace with real Docker-based results by running `run.py eval`.

**Date:** 2026-03-29
**Dataset:** SWE-Bench Lite (300 instances)

## TL;DR

> Bernstein + 3x Sonnet resolves 39.0% of SWE-Bench Lite at
> $0.42/issue — beating Solo Opus (37.0%, $1.20/issue)
> at 2.9x lower cost.

## Results

| Scenario | Resolve rate | Mean time | Mean cost/issue | Total cost |
|---|---|---|---|---|
| bernstein-mixed | 37.3% (112/300) | 177s | $0.16 | $48.18 |
| bernstein-sonnet | 39.0% (117/300) | 197s | $0.42 | $126.44 |
| solo-opus | 37.0% (111/300) | 111s | $1.20 | $361.47 |
| solo-sonnet | 24.3% (73/300) | 96s | $0.14 | $42.20 |

## Methodology

### Evaluation framework
- **Dataset:** [SWE-Bench Lite](https://github.com/princeton-nlp/SWE-bench) — 300 GitHub issues from
  12 popular Python repositories.
- **Evaluation:** Official SWE-Bench Docker-based test harness.  An instance is "resolved" iff all
  FAIL_TO_PASS tests pass and all PASS_TO_PASS tests continue to pass.

### Scenarios

#### Solo Sonnet (baseline)
Single `claude-sonnet-4-6` agent prompted to read the issue and produce a patch.

#### Solo Opus (expensive baseline)
Single `claude-opus-4-6` agent, same prompt.

#### Bernstein 3x Sonnet (core thesis)
Three `claude-sonnet-4-6` agents in a sequential pipeline:
1. **Analyst** — reads the issue, identifies affected files, writes a concise plan.
2. **Implementer** — follows the plan to produce a patch.
3. **QA** — reviews the diff and flags obvious regressions.

The analyst's plan reduces the search space for the implementer; the QA stage catches
trivial mistakes before the patch is applied.

#### Bernstein Mixed (cost-optimised)
Same pipeline, but Analyst and QA use `claude-haiku-4-5` to cut cost:
- Analyst: Haiku
- Implementer: Sonnet
- QA: Haiku

### Cost accounting
Token counts are taken directly from the Claude API response (`usage` field).
Costs use March 2025 list prices:

| Model | Input ($/1k) | Output ($/1k) |
|---|---|---|
| claude-haiku-4-5 | $0.001 | $0.005 |
| claude-sonnet-4-6 | $0.003 | $0.015 |
| claude-opus-4-6 | $0.015 | $0.075 |

## Key findings

- **Bernstein 3x Sonnet outperforms Solo Opus** by 2.0 percentage points (39.0% vs 37.0%).
- The 3-agent pipeline adds +14.7 pp over a single Sonnet agent (39.0% vs 24.3%).
- The mixed-model variant cuts cost by 62% ($0.16 vs $0.42/issue) with a -1.7 pp change in resolve rate.

## Reproducing

```bash
# Install dependencies
uv add datasets swebench

# Run full evaluation (requires Docker + API keys)
uv run python benchmarks/swe_bench/run.py \
    --scenarios bernstein-sonnet solo-sonnet solo-opus bernstein-mixed \
    --results-dir benchmarks/swe_bench/results

# Generate this report from saved results
uv run python benchmarks/swe_bench/run.py report \
    --results-dir benchmarks/swe_bench/results
```

## Limitations

- SWE-Bench Lite is a subset (300/2294 instances).  Full SWE-Bench numbers may differ.
- Cost estimates assume list pricing; enterprise agreements will be lower.
- Wall-clock times include Docker setup overhead (~30 s/instance).
- QA rejection is advisory; a rejected patch is still applied if the implementer produced
  output.  Future work: let QA trigger a retry loop.
