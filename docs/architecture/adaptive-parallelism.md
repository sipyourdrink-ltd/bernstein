# Adaptive Parallelism

**Why does my `max_agents` value drift at runtime?**

`max_agents` in `bernstein.yaml` is the *configured ceiling*, not a
constant. Bernstein runs a feedback controller that lowers the effective
ceiling when the run is breaking things or when CPU is overloaded, and
raises it back toward the configured max once both signals settle. The
result is a moving `effective_max_agents` value visible in dashboards
and metrics.

If you only have time for one sentence: **set `max_agents` as a
ceiling, not a target - the orchestrator will run fewer concurrent
agents on its own when error rate or CPU spikes**. The rest of this page
explains the rules, bounds, and how to opt out for deterministic runs.

---

## The problem: static `max_agents` is wrong

A flat `max_agents: 8` works if every task is uniform. In real runs
tasks mix:

- A few **light** tasks (a 30 s docs edit) - eight in parallel is fine.
- One **heavy** task (a long Opus call with a 90 s tool chain) - eight
  in parallel pin the CPU, the kernel starts swapping, every agent
  slows down.
- A **bad** task that fails repeatedly - running it eight times in
  parallel just produces eight failures faster.

Static caps either under-utilise (too low for light tasks) or thrash
(too high for heavy/bad ones). The right answer is dynamic: react to
*observed* signals.

---

## The solution: a feedback controller on `max_agents`

`core/orchestration/adaptive_parallelism.py` (`AdaptiveParallelism`
dataclass) tracks two windowed signals - task error rate and CPU load -
and re-evaluates `effective_max_agents` once per orchestrator tick. The
controller mutates the orchestrator's working `max_agents` value before
the spawner picks tasks for that tick:

```python
self._adaptive_parallelism = AdaptiveParallelism(configured_max=config.max_agents)
...
_effective_max = self._adaptive_parallelism.effective_max_agents()
self._config.max_agents = _effective_max
```

Source: `core/orchestration/orchestrator.py`.

The controller is purely additive - if you don't read its outputs,
nothing changes about the orchestrator loop except that `max_agents`
now drifts.

---

## Inputs (signals the controller reads)

Three live signals, one explicit override:

1. **Task error rate** - `record_outcome(success: bool)` called by the
   orchestrator after every terminal task transition. The controller
   keeps a sliding window of `(timestamp, success)` outcomes
   (`adaptive_parallelism.py`).
2. **CPU load** - read on every tick. On Unix, `os.getloadavg()[1]`
   (5-minute load average, normalised by `os.cpu_count()`). On Windows,
   `psutil.cpu_percent()` if available, else `0.0` (the rule
   effectively disables itself there). Source: `_get_cpu_percent()` at
   `adaptive_parallelism.py`.
3. **Time since startup** - used as a 120 s grace period during which
   CPU rules are skipped (boot-time spikes are normal).
4. **SLO error-budget cap** - an external override. The SLO subsystem
   can call `set_slo_constraint(max_agents)` to pin the controller to a
   hard ceiling when the error budget is depleted; clearing the cap is
   `set_slo_constraint(None)` (`adaptive_parallelism.py`).

Window size and thresholds are not magic numbers - they're declared
once in `core/defaults.py` (`ParallelismDefaults`) and reused.

---

## Algorithm

The controller is a deterministic state machine evaluated each tick.
Rules are checked in priority order; the first one that applies returns
immediately so high-priority signals can't be cancelled by lower ones:

1. **CPU overload (with 120 s startup grace).** If `cpu_percent` exceeds
   the threshold *and* the orchestrator has been running for more than
   120 s, halve `current_max` (floored at 1) and stash the prior value
   in `_pre_cpu_max` for restoration later. Reason logged:
   `"cpu_high (NN%)"`. Source: `_apply_cpu_overload_rule` at
   `adaptive_parallelism.py`.
2. **High error rate.** If error rate over the window exceeds 20% and
   `current_max > 1`, decrement by one. Reason logged:
   `"error_rate_high (NN%)"`. Source: `_apply_high_error_rule` at
   `adaptive_parallelism.py`.
3. **Sustained low error rate.** If error rate drops under 5% **and
   stays under 5% for 120 s** (the "sustain" window), increment by one
   up to the configured max. Reset the timer on every increment so each
   step requires another 120 s of clean burn-in. Source:
   `_apply_low_error_rule` at `adaptive_parallelism.py`.
4. **CPU recovery.** If CPU dropped back below the threshold and
   `_pre_cpu_max > current_max`, restore to the pre-spike level (capped
   at `configured_max`). Reason: `"cpu_recovered"`. Source:
   `_apply_cpu_recovery_rule` at `adaptive_parallelism.py`.
5. **SLO hard cap.** After all adaptive rules, the SLO constraint is
   enforced as a `min()` clamp - even if the controller wants more
   agents, the SLO budget can deny it (`adaptive_parallelism.py`).
6. **Minimum floor.** Never drop below `max(1, configured_max // 2)`
   except via CPU overload (early-returned above) or the explicit SLO
   cap. Prevents the system from crawling at one or two agents when
   five slots are available (`adaptive_parallelism.py`).

Each rule that fires writes a one-line `INFO`/`WARNING` log so the
trail is easy to read after a run.

---

## Bounds

| Bound | Source | Default | YAML override |
|-------|--------|---------|---------------|
| Configured ceiling | `bernstein.yaml: max_agents` | `7` | `max_agents: <int>` |
| Floor | `max(1, configured_max // 2)` | `3` for `max_agents=7` | implicit; not configurable |
| Error rate "high" | `PARALLELISM.error_rate_high` | `0.20` | `tuning.parallelism.error_rate_high` |
| Error rate "low" | `PARALLELISM.error_rate_low` | `0.05` | `tuning.parallelism.error_rate_low` |
| Low-error sustain | `PARALLELISM.low_error_sustain_s` | `120 s` | `tuning.parallelism.low_error_sustain_s` |
| CPU pause threshold | `PARALLELISM.cpu_pause_threshold` | `300.0` (3 cores pinned) | `tuning.parallelism.cpu_pause_threshold` |
| Window size | `PARALLELISM.window_s` | `600 s` | `tuning.parallelism.window_s` |

Source: `core/defaults.py`. Tunable via the `tuning.parallelism`
config branch - leave them alone unless your workload is unusual.

---

## Observability

Two surfaces:

- **Log lines.** Every adjustment writes one line at `INFO` (low-error
  ramp-up, cpu recovery) or `WARNING` (cpu overload, slo cap). Search
  for `"Adaptive parallelism:"` in the orchestrator log to reconstruct
  the trace.

- **Metrics.** Each tick the orchestrator records a
  `PARALLELISM_LEVEL` gauge with labels `configured_max`, `error_rate`,
  `cpu_percent`, `reason`. This is the time-series dashboards plot
  (`orchestrator.py`):

  ```python
  get_collector()._write_metric_point(
      MetricType.PARALLELISM_LEVEL,
      float(_effective_max),
      {
          "configured_max": str(_ap_status.configured_max),
          "error_rate": f"{_ap_status.error_rate:.3f}",
          "cpu_percent": f"{_ap_status.cpu_percent:.1f}",
          "reason": _ap_status.last_adjustment_reason,
      },
  )
  ```

- **Status snapshot.** `controller.status()` returns an
  `AdaptiveParallelismStatus` (`configured_max`, `current_max`,
  `error_rate`, `cpu_percent`, `last_adjustment_reason`,
  `window_size`). Used by the dashboard endpoints (`/status`,
  `/dashboard/data`) so operators can see why the orchestrator chose a
  given level.

---

## When to disable

Reach for "deterministic" mode when:

- **Compliance / replay runs** - bit-for-bit reproducibility requires
  a fixed `max_agents`. Drift breaks the WAL replay equivalence-check.
- **Benchmarks / regressions** - you want to measure adapter latency,
  not the controller.
- **Tests that assert exact concurrency** - set the configured max
  high so the floor pins the effective max to the same value (or hard-
  pin via `set_slo_constraint`).

There is no `enabled: false` switch - the controller is always
constructed by the orchestrator. To get static behaviour:

1. Set both `error_rate_high` and `error_rate_low` to extreme values
   (`1.0` and `0.0`) so neither rule fires, and `cpu_pause_threshold`
   far above any real load.
2. Or set `configured_max == floor` (i.e. tune `max_agents` so
   `max_agents // 2` is your target) - the controller then has no
   slack to play with.

In practice, leaving the controller on with sensible defaults is the
right call for almost every workload.

---

## Code pointers

| Concern | File |
|---------|------|
| Controller state machine | `src/bernstein/core/orchestration/adaptive_parallelism.py` |
| Defaults | `src/bernstein/core/defaults.py` (`ParallelismDefaults`) |
| Orchestrator integration | `src/bernstein/core/orchestration/orchestrator.py` |
| SLO budget integration | `set_slo_constraint()` at `adaptive_parallelism.py` |
| Status snapshot | `AdaptiveParallelismStatus` at `adaptive_parallelism.py` |
| Metric type | `MetricType.PARALLELISM_LEVEL` (consumed by `core/observability/`) |

See also: [`warm-pool.md`](warm-pool.md) (sizing latency, not width);
[`state-persistence.md`](state-persistence.md) (how outcomes feed the
WAL / replay).
