# 411 — Self-discovered feature tickets from codebase analysis

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** high
**Depends on:** [410]

## Problem
Evolution cycles currently focus on incremental improvements (fix tests, improve quality). The system should also discover missing features by analyzing the codebase architecture, comparing to DESIGN.md goals, and generating feature-level tickets with proper scoping.

## Implementation
1. Add `FeatureDiscovery` class to `src/bernstein/evolution/detector.py`:
   - Parse DESIGN.md for feature list, diff against implemented features
   - Analyze codebase for dead code paths (defined but never called)
   - Check for TODOs/FIXMEs that indicate planned work
   - Look for common patterns missing (e.g., retry logic, caching, rate limiting)
2. Feature ticket generation:
   - Score each opportunity by effort vs impact
   - Generate properly scoped tickets (small/medium, not "rewrite everything")
   - Write to `.sdd/backlog/open/` in standard ticket format
   - Include dependency analysis (which tickets block which)
3. Dedup against existing tickets (done, closed, open) by semantic similarity
4. Cap: max 5 feature tickets per evolution cycle to prevent backlog bloat

## Files
- src/bernstein/evolution/detector.py — add FeatureDiscovery
- src/bernstein/evolution/loop.py — integrate feature discovery
- tests/unit/test_feature_discovery.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_feature_discovery.py -x -q
- file_contains: src/bernstein/evolution/detector.py :: FeatureDiscovery
