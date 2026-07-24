# Cost anomaly detection

`CostAnomalyDetector` watches a run's budget burn rate and, when spend
crosses configured thresholds, logs a warning or stops the orchestrator from
spawning new agents. It is decoupled from `orchestrator.py` to avoid
circular imports and is wired into the orchestrator's slow tick path.

Source: `src/bernstein/core/cost/cost_anomaly.py` (`CostAnomalyDetector`),
`src/bernstein/core/tasks/models.py` (`CostAnomalyConfig`).

## What actually runs today

`CostAnomalyDetector` implements five detection rules, but only one is
currently invoked by the orchestrator:

| Rule | Method | Wired into the tick loop? |
|---|---|---|
| `burn_rate` | `check_tick()` → `_check_burn_rate()` | **Yes** - called every slow tick |
| `per_task_ceiling` | `check_task_completion()` | No - method exists, no caller |
| `token_ratio` | `check_task_completion()` | No - method exists, no caller |
| `retry_spiral` | `check_task_completion()` | No - method exists, no caller |
| `model_mismatch` | `check_spawn()` | No - method exists, no caller |

`check_task_completion()` and `check_spawn()` are public methods on
`CostAnomalyDetector` and are exercised by unit tests, but nothing in the
orchestrator currently calls them at task-completion or spawn time. Only
`check_tick()` (burn rate against the run's budget) is on the live path, via
`orchestrator.py`'s slow-tick branch:

```python
if _run_slow:
    for sig in self._anomaly_detector.check_tick(list(self._agents.values()), self._cost_tracker):
        self._handle_anomaly_signal(sig)
```

## Burn-rate detection (the live path)

Each slow tick, the detector computes `spent_usd / budget_usd * 100` from
the run's cost tracker and compares it against two percentage thresholds:

| Threshold | Default | Action |
|---|---|---|
| `budget_warn_pct` | 60% | log a warning |
| `budget_stop_pct` | 90% | `stop_spawning` |

`stop_spawning` sets `self._stop_spawning = True` on the orchestrator, which
halts new agent spawns; agents already running are not killed. Each signal
is cooled down independently per rule (`kill_agent`: no cooldown,
`stop_spawning`: 60s, `log`: 300s) so the same condition doesn't spam a
warning every tick.

## Baseline (used by the non-wired rules)

The detector maintains a rolling per-complexity-tier cost baseline
(`.sdd/metrics/cost_baseline.json`, last 50 tasks by default) with median
and p95 cost per tier, plus a median/p95 output/input token ratio. This
baseline is what `per_task_ceiling` and `token_ratio` would compare against
if their calling methods were wired in. It is **not** a Z-score model - the
comparisons are ratio-to-median and percentile-based
(`statistics.median`, nearest-rank percentile), computed from
`CostBaseline`/`TierStats`.

## Configuration

`CostAnomalyConfig` (`core/tasks/models.py`) defines every threshold:

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | master switch |
| `per_task_multiplier` | 3.0 | (unused rule) warn threshold vs tier median |
| `per_task_critical_multiplier` | 6.0 | (unused rule) kill threshold vs tier median |
| `budget_warn_pct` | 60.0 | burn-rate warn threshold |
| `budget_stop_pct` | 90.0 | burn-rate stop-spawning threshold |
| `token_ratio_max` | 5.0 | (unused rule) output/input ratio kill threshold |
| `token_ratio_min_tokens` | 5000 | (unused rule) minimum tokens before the ratio check applies |
| `retry_cost_multiplier` | 2.0 | (unused rule) cumulative-retry-cost multiplier |
| `baseline_window` | 50 | recent tasks kept for baseline statistics |
| `baseline_min_samples` | 5 | minimum samples per tier before ceiling checks would activate |

**This config is not currently exposed through `bernstein.yaml`.**
`OrchestratorConfig.cost_anomaly` is a dataclass field with
`default_factory=CostAnomalyConfig`, but the orchestrator's config
construction (`orchestrator.py`, the `OrchestratorConfig(...)` call built
from the parsed run seed) does not pass a `cost_anomaly` value through, so
every run uses the defaults above. Changing a threshold today means
constructing `CostAnomalyDetector` with a different `CostAnomalyConfig`
programmatically - there is no `bernstein.yaml` key or CLI flag for it yet.

## Output

Every signal (fired or not - only fired signals reach this path) is appended
to `.sdd/metrics/anomalies.jsonl` via `record_signal()`, one JSON object per
line with `rule`, `severity`, `action`, `agent_id`, `task_id`, `message`,
`details`, and `timestamp`. There is no CLI command or HTTP route that reads
this file back today; inspect it directly (`tail -f .sdd/metrics/anomalies.jsonl`)
or via the orchestrator's log warnings.

## Limitations

- Only burn-rate detection is live. Per-task cost ceilings, token-ratio
  spikes, retry-cost spirals, and model/complexity mismatches are
  implemented and unit-tested but not invoked by the orchestrator.
- Thresholds are fixed at their dataclass defaults; no `bernstein.yaml`
  surface exists to tune them per project.
- No CLI or HTTP surface reads `anomalies.jsonl` back; it is a write-only
  log today.

## Related

- [Cost-aware scheduling](cost-aware-scheduling.md) - the separate,
  configurable USD-ceiling policy layer that halts dispatch before a task
  starts (a different mechanism from this tick-based burn-rate check).
- [Cost optimization](cost-optimization.md) - general cost-control levers.
