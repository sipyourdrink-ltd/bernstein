# 310 — Wire self-evolution feedback loop end-to-end

**Role:** backend
**Priority:** 2
**Scope:** large
**Complexity:** high
**Estimated minutes:** 90
**Depends on:** [300, 301, 306, 307, 308]

## Problem

The evolution components exist individually but aren't wired into a working end-to-end loop. DESIGN.md and ADR-003 specify a closed loop:

```
Task Completion → Metrics Collection → Analysis → Upgrade Decision → Execution → Verification → Git Commit
```

Currently:
- `evolution.py` has `EvolutionCoordinator` with analysis + proposal generation
- `orchestrator.py` calls `evolution_coordinator.evaluate()` every N ticks
- `upgrade_executor.py` can apply changes with rollback
- But the full data flow isn't verified end-to-end

## Implementation

1. **Verify metrics flow**: Task completion in orchestrator → `MetricsCollector.record_task()` → `.sdd/metrics/tasks.jsonl`
2. **Verify analysis trigger**: Orchestrator tick → `EvolutionCoordinator.evaluate()` → reads metrics → runs analysis
3. **Verify proposal creation**: Analysis finds opportunity → creates `UpgradeProposal` → writes to `.sdd/upgrades/pending.json`
4. **Verify execution**: Approved proposal → `UpgradeExecutor.execute()` → applies change → verifies → git commits
5. **Verify rollback**: Failed verification → `UpgradeExecutor.rollback()` → restores backup
6. **Add integration test**: Mock a complete cycle from task completion through upgrade application

## Verification

```bash
uv run pytest tests/unit/test_evolution.py tests/unit/test_orchestrator.py -x -q
uv run pytest tests/ -x -q
```

## Owned files
- src/bernstein/core/orchestrator.py
- src/bernstein/core/evolution.py
- tests/unit/test_evolution_integration.py (new)

## Completion signals
- type: test_passes
  value: uv run pytest tests/unit/test_evolution_integration.py -x -q
- type: path_exists
  path: tests/unit/test_evolution_integration.py
