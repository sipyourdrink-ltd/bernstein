# Decompose evolution.py into risk-stratified modules

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
evolution.py is 923 lines combining metrics collection, analysis, proposal generation,
and execution in one file. Research on self-evolving systems (AlphaEvolve, DGM, SEAL)
shows these concerns MUST be separated along risk boundaries to enable safe auto-apply
for low-risk changes while keeping human gates for high-risk ones.

## Target structure
```
src/bernstein/evolution/
├── __init__.py
├── aggregator.py     # MetricsAggregator: EWMA + CUSUM + BOCPD
├── detector.py       # OpportunityDetector: threshold rules → opportunities
├── proposals.py      # ProposalGenerator: LLM-driven change synthesis
├── sandbox.py        # SandboxValidator: git worktree + pytest
├── gate.py           # ApprovalGate: confidence routing (L0/L1/L2/L3)
├── applicator.py     # ChangeApplicator: git branch, merge, rollback
├── invariants.py     # InvariantsGuard: hash-lock safety files on boot
├── circuit.py        # CircuitBreaker: halt conditions, rate limits
└── types.py          # Shared types: RiskLevel, UpgradeProposal, etc.
```

## Acceptance criteria
- All existing evolution.py classes migrated to new module structure
- Risk levels defined as enum: L0_CONFIG, L1_TEMPLATE, L2_LOGIC, L3_STRUCTURAL
- Each module has clear single responsibility
- Existing tests in test_evolution.py still pass (update imports)
- New __init__.py re-exports public API for backward compat

## Files
- src/bernstein/core/evolution.py (source, read-only reference)
- src/bernstein/evolution/ (new package)
- tests/unit/test_evolution.py (update imports)

## Completion signals
- path_exists: src/bernstein/evolution/__init__.py
- path_exists: src/bernstein/evolution/invariants.py
- path_exists: src/bernstein/evolution/circuit.py
- test_passes: uv run pytest tests/unit/test_evolution.py -x -q
