# Bernstein Benchmark Status and Framework Context

> **NOTE:** Public numeric framework-vs-framework rankings are intentionally withheld.
> Bernstein publishes benchmark claims only from verified SWE-Bench eval artifacts,
> and current public scope is Bernstein vs solo baselines rather than CrewAI/LangGraph tables.

**Date:** 2026-03-31

## TL;DR

> Bernstein keeps CrewAI and LangGraph on this page as architecture context.
> Public benchmark publication is gated on verified SWE-Bench eval artifacts, not simulated or estimated rows.

## Architecture Comparison

| Feature | Bernstein | CrewAI | LangGraph |
|---------|-----------|--------|-----------|
| Orchestration via LLM | No | Yes | Yes |
| Scheduling overhead | none (deterministic code) | present (LLM-based routing) | present (LLM-based routing) |
| Works with any CLI agent | Yes | No | No |
| State persistence | file-based (.sdd/) | in-memory (process lifetime) | checkpoint store (LangChain) |

## Public Benchmark Publication Status

| System | Public numeric benchmark status | Notes |
|--------|--------------------------------|-------|
| Bernstein | Published only from verified `benchmarks/swe_bench/run.py eval` artifacts | Public v1 scope is Bernstein vs solo baselines on SWE-Bench Lite. |
| CrewAI | Withheld from public numeric tables | No Bernstein-owned live harness is published yet. Architecture context only. |
| LangGraph | Withheld from public numeric tables | No Bernstein-owned live harness is published yet. Architecture context only. |

## Key Findings

- Bernstein keeps orchestration in deterministic Python code, which removes the need for a manager-model control plane.
- CrewAI and LangGraph stay in this report as architecture context only. Public numeric rankings are withheld until Bernstein can reproduce them under a Bernstein-owned live harness.
- Internal benchmark preview data may exist, but public publication is limited to verified SWE-Bench eval artifacts and Bernstein-vs-solo baselines.
