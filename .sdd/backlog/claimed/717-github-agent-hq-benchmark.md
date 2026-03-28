# 717 — Benchmark vs GitHub Agent HQ

**Role:** docs
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** #701

## Problem

GitHub Agent HQ (launched Feb 2026) lets developers run Claude, Codex, and Copilot simultaneously on the same task. This is exactly what Bernstein does — but Agent HQ is proprietary and GitHub-only. A direct comparison showing Bernstein is open-source, model-agnostic, CLI-native, and cost-optimized would be the most viral blog post we can write. "The open-source alternative to GitHub Agent HQ" is an SEO goldmine.

## Design

### Blog post structure
1. "GitHub validated multi-agent orchestration. Here's the open-source version."
2. Feature comparison table (honest — show where Agent HQ wins too)
3. Cost comparison: same task on Agent HQ vs Bernstein with model cascade
4. Architecture comparison: proprietary vs deterministic Python
5. "When to use Agent HQ vs Bernstein" (builds trust)

### Benchmark
Run identical tasks on both platforms, measure wall-clock time, cost, test pass rate.

## Files to modify

- `docs/compare/bernstein-vs-github-agent-hq.md` (new)
- `benchmarks/` (add Agent HQ comparison data)

## Completion signal

- Blog post exists with real benchmark data
- Comparison page linked from README
- Publishable on HN, Reddit, Dev.to
