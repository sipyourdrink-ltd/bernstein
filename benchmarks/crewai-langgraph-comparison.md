# Bernstein, CrewAI, and LangGraph: Architecture Context

This document no longer publishes numeric cross-framework benchmark claims.

Why:

- Bernstein-owned verified public benchmark publication is currently limited to SWE-Bench Lite.
- Public v1 scope is Bernstein vs real single-agent baselines.
- CrewAI and LangGraph remain useful comparison targets, but Bernstein is not publishing percentage wins or cost claims against them until a Bernstein-owned live harness exists.

## Architecture comparison

| Feature | Bernstein | CrewAI | LangGraph |
|---|---|---|---|
| Orchestration control plane | Deterministic Python scheduler | Manager LLM plus worker agents | Graph runtime with model-driven nodes |
| CLI agent support | Yes | No | Not the primary abstraction |
| State model | File-based `.sdd/` state | In-process workflow state | Checkpoint store |
| Primary strength | CLI-agent orchestration and observability | Agent-role workflows | App-embedded graph workflows |

## Public benchmark publication status

| System | Public numeric status | Notes |
|---|---|---|
| Bernstein | Published only from verified `benchmarks/swe_bench/run.py eval` artifacts | Current public scope is Bernstein vs solo baselines on SWE-Bench Lite |
| CrewAI | Withheld from public numeric tables | No Bernstein-owned live harness is published yet |
| LangGraph | Withheld from public numeric tables | No Bernstein-owned live harness is published yet |

## Reproducing Bernstein's public path

```bash
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py
```
