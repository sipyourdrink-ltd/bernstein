# 726 — SYNAPSE-Inspired Adaptive Governance for Evolution

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Depends on:** none

## Problem

Bernstein's evolution system (`--evolve`) uses fixed heuristics to decide what to improve: it looks at test pass rates, lint errors, and static metrics. But "what matters" changes over time — early in a project, test coverage matters most; later, performance and security dominate. The system has no way to dynamically re-weight its own success criteria based on project context and past outcomes.

SYNAPSE (a prior research framework by the same author — see `archive/synapse/`) solved this with **adaptive governance**: an agent that dynamically adjusts its own evaluation metrics using LLM-driven context analysis and multi-criteria decision making. The SYNAPSE experiment showed +28% improvement in composite performance score and 35% risk reduction vs a static-metrics agent over 10K scenarios.

## Design

Port SYNAPSE's three core innovations into Bernstein's evolution pipeline:

### 1. Adaptive Metric Weights

Instead of fixed weights for evolution scoring, maintain a dynamic weight vector that the system adjusts each cycle:

```python
@dataclass
class EvolutionWeights:
    test_coverage: float = 0.30
    lint_score: float = 0.15
    type_safety: float = 0.15
    performance: float = 0.10
    security: float = 0.15
    maintainability: float = 0.15
```

Each evolution cycle, before scoring proposals, the system:
1. Summarizes project context (current metrics, recent failures, codebase size, active areas)
2. Asks the planning LLM (already used for task decomposition): "Given this context, which metrics should be weighted higher for the next improvement cycle?"
3. Adjusts weights based on the response
4. Logs the weight changes to `.sdd/metrics/evolution_weights.jsonl`

### 2. Strategic Risk Score (SRS)

Before applying any evolution proposal, compute a risk score:

```python
@dataclass
class RiskAssessment:
    code_complexity_delta: float  # did the change increase complexity?
    test_coverage_delta: float    # did coverage improve or regress?
    regression_potential: float   # how many existing tests could break?
    blast_radius: int             # how many files touched?
    composite_risk: float         # weighted combination
```

Proposals with high risk get routed to sandbox verification. Low-risk proposals can be fast-tracked. This replaces the current binary "apply or reject" with a graduated risk-aware pipeline.

### 3. Decision Log / Governance Trail

Every evolution decision is logged with full provenance:

```json
{
  "cycle": 42,
  "timestamp": "2026-03-28T17:00:00Z",
  "weights_before": {"test_coverage": 0.30, "security": 0.15},
  "weights_after": {"test_coverage": 0.20, "security": 0.30},
  "weight_change_reason": "3 security issues found in last 5 cycles, coverage already at 89%",
  "proposals_evaluated": 5,
  "proposals_applied": 2,
  "risk_scores": [0.12, 0.08, 0.45, 0.67, 0.23],
  "outcome_metrics": {"pps_delta": +0.04, "srs_delta": -0.12}
}
```

This makes the evolution system auditable and debuggable — you can trace WHY the system decided to focus on security over coverage at cycle 42.

### Integration Points

- **`src/bernstein/core/evolution.py`** — add `AdaptiveGovernor` class with weight adjustment
- **`src/bernstein/evolution/`** — add `risk.py` for SRS computation, `governance.py` for decision logging
- **`src/bernstein/core/orchestrator.py`** — in evolve mode, call governor before scoring proposals
- **`.sdd/metrics/evolution_weights.jsonl`** — weight history
- **`.sdd/metrics/governance_log.jsonl`** — decision trail

### What NOT to port from SYNAPSE

- PROMETHEE II / ELECTRE Tri-C — overkill for this context. Simple weighted scoring is sufficient.
- PPO-CRL policy layer — reinforcement learning is unnecessary when we have an LLM for adaptation.
- The drone simulation — obviously.

## Files to modify

- `src/bernstein/evolution/governance.py` (new)
- `src/bernstein/evolution/risk.py` (new)
- `src/bernstein/core/evolution.py` (integrate governor)
- `src/bernstein/core/orchestrator.py` (call governor in evolve loop)
- `tests/unit/test_governance.py` (new)
- `tests/unit/test_risk.py` (new)

## Completion signal

- Evolution cycles log weight adjustments with reasons
- Risk score computed for each proposal
- High-risk proposals routed to sandbox, low-risk fast-tracked
- Governance trail in `.sdd/metrics/governance_log.jsonl`
- Tests pass for weight adaptation, risk scoring, and decision logging

## Prior art

Based on the SYNAPSE framework (archive/synapse/) — an adaptive software engineering research prototype that demonstrated +28% PPS improvement and 35% risk reduction through dynamic metric adaptation. The core insight: static evaluation criteria produce diminishing returns; adaptive criteria converge faster on what actually matters for a given project's current state.
