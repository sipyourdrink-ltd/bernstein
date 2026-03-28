# 719 — "30 Days of Self-Evolution" Content Piece

**Role:** docs
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Bernstein's `--evolve` mode is genuinely unique — no competitor has shipped self-evolution as a core feature. "We let Bernstein improve itself for 30 days. Here's what happened." is content that no competitor can produce. This is our unfair advantage for virality.

## Design

### Data collection
Run `bernstein --evolve` on the Bernstein repo for 30 days. Record:
- Number of self-generated tickets
- Number completed successfully
- Code changes (LOC added/removed)
- Test count progression
- Cost per day
- Notable improvements (features it invented, bugs it found)

### Blog post
"An AI agent orchestrator that improves itself: 30 days of Bernstein's self-evolution"
- Day 0: state of the codebase
- Day 10: first surprising improvements
- Day 20: emergent behaviors
- Day 30: final state comparison
- Total cost, total commits, total test delta
- Honest about failures and weird behaviors

### Artifacts
- Time-lapse visualization of git activity
- Cost curve over 30 days
- Before/after code quality metrics

## Files to modify

- `docs/blog/self-evolution-30-days.md` (new)
- `docs/blog/` (new directory)

## Completion signal

- 30-day evolution run completed with data
- Blog post written with real data and honest analysis
- Publishable on HN, Reddit, Dev.to
