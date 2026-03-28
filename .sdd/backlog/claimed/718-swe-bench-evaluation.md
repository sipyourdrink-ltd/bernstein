# 718 — SWE-Bench Evaluation with Scaffolding Thesis

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** large
**Depends on:** none

## Problem

SWE-Bench Pro proved that scaffolding matters more than model weights: same model scores 23% with basic scaffold vs 45%+ with optimized scaffold. That 22-point swing dwarfs model differences. This is Bernstein's core thesis — the orchestration layer matters more than the model. We need numbers to prove it.

If Bernstein + cheap model beats expensive single model on SWE-Bench, that's the most compelling marketing asset possible. "Bernstein + Sonnet beats solo Opus" would go viral.

## Design

### Evaluation harness
- Run SWE-Bench Lite (300 issues) with:
  1. Single Claude Sonnet agent (baseline)
  2. Single Claude Opus agent (expensive baseline)
  3. Bernstein 3-agent with all Sonnet (our thesis)
  4. Bernstein 3-agent with mixed models (cost-optimized)
- Measure: resolve rate, wall-clock time, total cost

### Expected narrative
"Bernstein + 3 Sonnet agents resolves 38% of SWE-Bench at $0.42/issue. Solo Opus resolves 35% at $1.20/issue. Multi-agent orchestration is cheaper AND better."

## Files to modify

- `benchmarks/swe_bench/` (new)
- `benchmarks/swe_bench/run.py`
- `benchmarks/swe_bench/results/`

## Completion signal

- SWE-Bench Lite evaluation completes
- Results show multi-agent advantage
- Publishable as blog post with methodology
