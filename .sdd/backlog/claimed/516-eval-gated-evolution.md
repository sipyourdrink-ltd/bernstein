# 516 — Eval-gated evolution: only apply changes that improve eval scores

**Role:** architect
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** high
**Depends on:** [515]

## Problem
Self-evolution currently applies changes that pass tests. But passing tests != improving quality. A prompt change might pass tests while making agents slower, more expensive, or less reliable. The eval harness (#515) provides the quality signal — this ticket wires it into the evolution gate.

## Design: Eval as CI Gate for Evolution

### Current flow
```
Detect opportunity → Generate proposal → Sandbox (run tests) → Gate (tests pass?) → Apply
```

### New flow
```
Detect opportunity → Generate proposal → Sandbox (run tests) →
  Gate 1 (tests pass?) → Eval (run golden benchmark) →
  Gate 2 (score >= baseline?) → Apply + update baseline
```

### Implementation

#### 1. Baseline tracking
After each successful eval run, store the score as baseline:
`.sdd/eval/baseline.json`:
```json
{
  "score": 0.72,
  "components": {"task_success": 0.85, "code_quality": 0.78, ...},
  "timestamp": "2026-03-28T12:00:00Z",
  "config_hash": "abc123"
}
```

#### 2. Evolution gate integration
In `src/bernstein/evolution/gate.py`, after sandbox passes:
1. Apply proposed changes to sandbox worktree
2. Run `EvalHarness.run(tier="smoke")` in the sandbox (smoke tier only — fast)
3. Compare score vs baseline
4. **Accept**: if score >= baseline - 0.02 (allow tiny regression tolerance)
5. **Reject**: if score < baseline - 0.02, log reason and discard proposal
6. **Promote**: if score > baseline + 0.05, update baseline

#### 3. Regression prevention
The multiplicative Safety gate (from #515) means: if a proposal causes ANY test regression in eval, Safety = 0.0, total score = 0.0, proposal auto-rejected. No exception.

#### 4. Eval budget management
Running eval on every proposal is expensive. Tiered approach:
- L0 proposals (config tweaks): skip eval, only tests
- L1 proposals (prompt changes): run smoke eval only (~5 tasks, ~$0.50)
- L2 proposals (routing logic): run standard eval (~15 tasks, ~$2.00)
- L3 proposals (source changes): blocked anyway

#### 5. Evolution metrics from eval
Track eval scores across evolution cycles:
`.sdd/metrics/eval_trajectory.jsonl`:
```json
{"cycle": 14, "proposal": "tweak-backend-prompt", "baseline": 0.72, "proposed": 0.75, "accepted": true}
{"cycle": 15, "proposal": "reduce-batch-size", "baseline": 0.75, "proposed": 0.71, "accepted": false}
```

This becomes the best marketing asset: "Bernstein improved itself from 0.55 to 0.82 over 30 cycles."

## Files
- src/bernstein/evolution/gate.py — add eval gate
- src/bernstein/evolution/loop.py — wire eval into cycle
- .sdd/eval/baseline.json — tracked baseline
- .sdd/metrics/eval_trajectory.jsonl — eval over time
- tests/unit/test_eval_gate.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_eval_gate.py -x -q
- file_contains: src/bernstein/evolution/gate.py :: eval_gate
