# Cost anomaly detection

`CostAnomalyDetector` watches a run's budget burn rate and, when spend
crosses configured thresholds, logs a warning or stops the orchestrator from
spawning new agents. It is decoupled from `orchestrator.py` to avoid
circular imports and is wired into slow ticks, single-task spawn checks and
task completion.

Source: `src/bernstein/core/cost/cost_anomaly.py` (`CostAnomalyDetector`),
`src/bernstein/core/tasks/models.py` (`CostAnomalyConfig`).

## What actually runs today

`CostAnomalyDetector` implements five detection rules:

| Rule | Method | Wired into the tick loop? |
|---|---|---|
| `burn_rate` | `check_tick()` → `_check_burn_rate()` | **Yes** - called every slow tick |
| `per_task_ceiling` | `check_task_completion()` | Yes - task completion |
| `token_ratio` | `check_task_completion()` | Yes - task completion |
| `retry_spiral` | `check_task_completion()` | Yes - task completion with retry lineage |
| `model_mismatch` | `check_spawn()` | Yes - single-task batches after task-level routing |

Completion checks receive the collector's final cost and prompt/completion token
counts and update the rolling baseline. The private wiring translates
`Complexity.LOW/MEDIUM/HIGH` to the detector's existing `small/medium/large`
tiers. Retry lineage comes from `metadata.original_task_id`; typed retry counts
and legacy `[RETRY ...]` titles also identify retries.

Before a single-task spawn, a private spawner observer checks the resolved
`ModelConfig`, after task, role-policy, template and default model selection.
It also sees the configuration used for provider failover, bridge and resume
attempts. Routing and task pins are unchanged. The current detector emits
log-only signals on this path; all signals dispatch through the normal handler.
Multi-task batches remain outside the advisory check.

The detector's existing `enabled` default is **true**. Setting it explicitly to
`false` makes the new checks no-ops as well. Expected data/IO failures in a check
log a warning with task context and leave scheduling/completion running.

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
warning every tick. `Orchestrator.resume_spawning()` is the manual clear path
after the operator acknowledges the anomaly; no automatic-resume signal exists.

## Rolling baseline

The detector maintains a rolling per-complexity-tier cost baseline
(`.sdd/metrics/cost_baseline.json`, last 50 tasks by default) with median
and p95 cost per tier, plus a median/p95 output/input token ratio. This
baseline is used by per-task ceiling checks; token-ratio checks compare the
measured ratio to the configured bound. It is **not** a Z-score model - the
comparisons are ratio-to-median and percentile-based
(`statistics.median`, nearest-rank percentile), computed from
`CostBaseline`/`TierStats`.

## Configuration

`CostAnomalyConfig` (`core/tasks/models.py`) defines every threshold:

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | master switch |
| `per_task_multiplier` | 3.0 | warn threshold vs tier median |
| `per_task_critical_multiplier` | 6.0 | kill threshold vs tier median |
| `budget_warn_pct` | 60.0 | burn-rate warn threshold |
| `budget_stop_pct` | 90.0 | burn-rate stop-spawning threshold |
| `token_ratio_max` | 5.0 | output/input ratio kill threshold |
| `token_ratio_min_tokens` | 5000 | minimum tokens before the ratio check applies |
| `retry_cost_multiplier` | 2.0 | cumulative-retry-cost multiplier |
| `baseline_window` | 50 | recent tasks kept for baseline statistics |
| `baseline_min_samples` | 5 | minimum samples per tier before ceiling checks activate |

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

- Advisory spawn checks cover single-task batches; multi-task cost/model
  attribution remains outside this check.
- Completion anomalies are evaluated after the task finishes. A `kill_agent`
  action needs a live agent identity; completion signals carrying only a task
  identity are recorded rather than terminating an already-reaped session.
- Thresholds are fixed at their dataclass defaults; no `bernstein.yaml`
  surface exists to tune them per project.
- No CLI or HTTP surface reads `anomalies.jsonl` back; it is a write-only
  log today.

## Related

- [Cost-aware scheduling](cost-aware-scheduling.md) - the separate,
  configurable USD-ceiling policy layer that halts dispatch before a task
  starts (a different mechanism from this tick-based burn-rate check).
- [Cost optimization](cost-optimization.md) - general cost-control levers.
