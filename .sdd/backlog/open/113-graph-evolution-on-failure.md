# Implement failure-driven evolution (inspired by Hive)

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** high

## Background
Hive captures failure data and evolves the agent graph through the coding agent,
redeploying automatically. Key insight: failures are the best signal for what to
improve. Currently Bernstein creates fix tasks when janitor fails, but doesn't
learn from patterns of failure.

## What to implement
1. **Failure pattern detection**: When the same task type fails 3+ times with
   similar error patterns, the MetricsAggregator flags it as a systematic issue.
2. **Automatic prompt annealing**: If a role's tasks consistently fail, generate
   a proposal to improve that role's system prompt template. Use the failure logs
   as context for the LLM to suggest improvements.
3. **Routing evolution**: If a specific model consistently fails on a task type
   but another succeeds, propose a routing rule change (L0 config).
4. **Failure memory**: Append failure patterns to .sdd/evolution/failures.jsonl
   so the evolution engine can reference historical failures when proposing changes.

## Hive patterns to adapt
- "Guardian node" concept: a watchdog that monitors agent health → our CircuitBreaker
- "Graph evolution through coding agent" → our ProposalGenerator analyzing failure JSONL
- "Automatic redeployment" → our L0/L1 auto-apply after sandbox validation

## Files
- src/bernstein/evolution/detector.py (new — opportunity detection from failures)
- src/bernstein/evolution/aggregator.py (add failure pattern analysis)
- tests/unit/test_failure_evolution.py (new)

## Completion signals
- path_exists: src/bernstein/evolution/detector.py
- test_passes: uv run pytest tests/unit/test_failure_evolution.py -x -q
