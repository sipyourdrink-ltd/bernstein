# Implement the autoresearch evolution loop

**Role:** backend
**Priority:** 2 (normal)
**Scope:** large
**Complexity:** high

## Problem
The autoresearch pattern (Karpathy, March 2026, 50k+ stars) is the proven minimum
viable self-improvement loop. It found 15-20 improvements on hand-tuned code overnight.
Core: editable asset + scalar metric + time-boxed cycle.

## The loop (simplified)
```python
while within_evolution_window():
    metrics = load_recent_metrics(last_n=100)
    baseline = run_benchmark_suite(current_config)
    proposal = generate_proposal(current_config, metrics)  # LLM call, ~$0.05
    candidate = apply_in_sandbox(current_config, proposal)
    score = run_benchmark_suite(candidate)
    if score > baseline and passes_safety_checks():
        git_commit(candidate, proposal, delta=score-baseline)
        log_success(proposal, delta)
    else:
        discard(proposal, delta=score-baseline)
        log_failure(proposal, reason)
```

Target: 12 experiments per hour in 5-minute cycles.

## Implementation
- CLI: `bernstein evolve run [--window 2h] [--max-proposals 24]`
- Connects: MetricsAggregator → ProposalGenerator → SandboxValidator → ApprovalGate
- ProposalGenerator uses Claude Sonnet (cheap) to analyze failure patterns and suggest
  config/template tweaks. Cost: ~$0.05 per proposal.
- Only L0 and L1 changes in automated loop
- L2+ proposals saved for human review
- All results logged to .sdd/evolution/experiments.jsonl

## Depends on
- 100 (evolution module decomposition)
- 101 (14-field metrics)
- 102 (invariants guard)
- 103 (circuit breaker)
- 104 (approval gate)
- 105 (sandbox validator)
- 107 (benchmark suite)

## Files
- src/bernstein/evolution/loop.py (new)
- src/bernstein/cli/main.py (add evolve run command)
- tests/unit/test_evolution_loop.py (new)

## Completion signals
- path_exists: src/bernstein/evolution/loop.py
- test_passes: uv run pytest tests/unit/test_evolution_loop.py -x -q
- file_contains: src/bernstein/evolution/loop.py :: autoresearch
