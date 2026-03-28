# 417 — Evolution observability: bernstein evolve --status and export

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** medium

## Problem
Self-evolution is Bernstein's most distinctive feature but it's invisible. Evolution writes to JSONL files but there's no way for users (or outsiders evaluating the project) to see improvement trajectories. The dashboard shows task status, not evolution history. This is the best marketing asset we're not using.

## Implementation
1. `bernstein evolve --status`: shows evolution history as a rich table (cycle, focus, proposals, accepted/rejected, delta metrics, cumulative improvement)
2. `bernstein evolve --export [path]`: generates a static HTML or markdown report with:
   - Improvement trajectory chart (text-based sparkline or ASCII chart)
   - Per-cycle breakdown: what was proposed, what was accepted, what improved
   - Before/after metrics comparison
   - Total cost of evolution
3. Data source: `.sdd/metrics/evolve_cycles.jsonl` + `.sdd/evolution/experiments.jsonl`

## Files
- src/bernstein/cli/main.py — extend evolve command group
- src/bernstein/evolution/report.py (new) — report generation
- tests/unit/test_evolve_report.py (new)

## Completion signals
- file_contains: src/bernstein/cli/main.py :: evolve.*status
- file_contains: src/bernstein/evolution/report.py :: EvolutionReport
