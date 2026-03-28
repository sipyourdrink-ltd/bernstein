# 728 — Unify Metrics Collectors

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein has two separate metrics paths: operational metrics (task timing, cost, pass rates in `src/bernstein/core/metrics.py`) and evolution metrics (`src/bernstein/evolution/types.py` with `MetricsRecord`). They use different formats, different storage patterns, and don't feed into each other. This means evolution decisions can't see operational data, and the cost dashboard can't see evolution quality metrics.

## Design

Bridge the two metrics systems:

### Unified collector interface
```python
class UnifiedCollector:
    """Single collector that writes both operational and evolution metrics."""
    def record_task(self, task_id, agent_id, model, tokens, cost, duration, outcome): ...
    def record_evolution(self, cycle, proposals, applied, risk_scores): ...
    def query(self, metric_type, since, until) -> list[MetricRecord]: ...
```

### Storage
Single JSONL format with a `metric_type` discriminator:
```json
{"metric_type": "task_completion", "task_id": "T-001", "cost_usd": 0.12, ...}
{"metric_type": "evolution_cycle", "cycle": 42, "proposals": 5, ...}
```

### Migration
- `metrics.py` `get_collector()` wraps the unified collector
- Evolution code writes through the same collector
- Existing JSONL files remain readable (backwards compatible)

## Files to modify

- `src/bernstein/core/metrics.py` (add unified interface)
- `src/bernstein/evolution/types.py` (use unified collector)
- `src/bernstein/core/orchestrator.py` (wire unified collector)
- `tests/unit/test_metrics.py` (extend)

## Completion signal

- Single collector handles both operational and evolution metrics
- Evolution can query operational data (task costs, pass rates)
- Cost dashboard can show evolution quality trends
