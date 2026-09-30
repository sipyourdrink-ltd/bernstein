# Orchestrator hardening primitives

Three primitives - concurrency limits, deadline enforcement, and a
budget guard - are wired into the orchestrator runtime so they
engage on every run rather than living as off-by-default research
code. Together they bound the worst-case behaviour of a single run:
how many agents can be live at once, how long any single task can
hold the floor, and how much spend the run can rack up before the
loop aborts.

## Why it exists

Without these primitives the orchestrator's failure modes drift
into operator headaches: a runaway error loop spawns one agent per
tick, a stuck task burns wall-clock waiting on a hung subprocess,
or a routing accident sends every task to the most expensive model.
Each primitive is a hard cap with a clear signal so the run fails
loudly and quickly instead of degrading silently.

## Primitives

### 1. Concurrency limits

The orchestrator caps live agents per run. The cap has two layers:

- A configured ceiling (`max_agents`, env var
  `BERNSTEIN_MAX_AGENTS`) sets the upper bound.
- An adaptive layer monitors the recent task outcome window and
  the host CPU load and lowers the effective ceiling when the
  error rate is high or the host is saturated.

Default behaviour:

| Condition | Effect |
|---|---|
| Error rate > 20% over the recent window | Effective max drops by 1 (floor 1). |
| Error rate < 5% sustained ~2 min (within the 10 min window) | Effective max raises by 1 (up to configured max). |
| Load average per CPU above the pause threshold | Spawning paused (`effective_max = 0`) until load drops. |

Surface: the effective ceiling is recorded every tick as the
`parallelism_level` metric (metric collector; the Datadog exporter maps it
to a gauge).

### 2. Deadline enforcement

Every task carries an optional deadline. The orchestrator runs a
deadline check each tick:

- A warning event (`task.deadline_warning`) fires when a task is
  inside the warning window.
- A hard event (`task.deadline_exceeded`) fires when a task is
  past its deadline. The task is failed so the retry path can
  apply deadline-aware escalation, and a meta message is appended
  for the next agent so it knows the previous attempt was killed
  on a deadline rather than a code error.

The retry / decompose path treats deadline failures as a distinct
class so backoff and model escalation can react accordingly.

### 3. Budget guard

The cumulative routed spend is tracked per run. Three thresholds
fire in sequence:

| Threshold | Default | Action |
|---|--:|---|
| Warn | 80% | One-time `Budget warning` log line from the cost tracker; `should_warn` is exposed in the costs/status API. The orchestrator's default `BudgetPolicy` also returns PAUSE from 80%, holding new spawns (running agents continue). |
| Critical | 95% | One-time `BUDGET CRITICAL` log line; no action of its own. (From 90% the default `BudgetPolicy` returns DOWNGRADE_MODEL: ready tasks are rewritten to a cheaper model tier and spawning resumes on that tier.) |
| Stop | 100% | `should_stop` flips; the orchestrator drains live agents and aborts. |

The cap source resolves with the following precedence:

1. `BERNSTEIN_MAX_COST_USD` env var (set by the
   `bernstein run --max-cost-usd N` flag).
2. `.sdd/runtime/run_config.json` (`run_config_value`).
3. `bernstein.yaml` `budget` (`seed_value`, surfaced as `seed.budget_usd`).
4. Default `0.0` (= unlimited).

Non-positive values mean "unlimited" and are normalised to `0.0`.
Invalid env values are logged at warning level so a typo never
silently disables the guard.

## Configuration

| Knob | Default | Controls |
|---|---|---|
| `BERNSTEIN_MAX_AGENTS` | configured max | Hard ceiling on parallel agents per run. |
| `BERNSTEIN_MAX_COST_USD` | unset | Hard cap on cumulative routed spend per run. |
| `budget` (`bernstein.yaml`) | unset | Per-run budget read from `bernstein.yaml`. |
| `defaults.PARALLELISM.error_rate_high` | `0.20` | Error rate above which adaptive parallelism shrinks. |
| `defaults.PARALLELISM.cpu_pause_threshold` | `300.0` | Load-average percent-per-CPU above which spawning pauses (default ~3 pinned cores). |

## Metrics

The metric collector records `parallelism_level` (current effective max
agent count) each tick. Deadline outcomes are reported as the
`task.deadline_warning` / `task.deadline_exceeded` events rather than
counters, and budget state is exposed through the cost tracker's
`BudgetStatus` (`should_warn`, `should_stop`).

### 4. Commit-completion check (retry-with-continuation)

A common failure mode of CLI coding agents is exiting with success
while the workspace is unchanged: the assistant reports completion but
no new commit landed. The orchestrator now snapshots HEAD before the
adapter spawn and compares after the process exits. When the agent
exited cleanly (exit code 0) but HEAD did not move, the orchestrator
launches a single continuation retry through the adapter's
session-resume primitive and appends a corrective nudge:

> You exited successfully but the workspace has no new commit. Either
> commit your work or explain in plain prose why no commit was needed
> for this task.

The retry path is gated by the adapter's
`supports_session_continuation` flag. Adapters that opt in (Claude
Code today; more to follow) expose a `continuation_args(session_id)`
method that returns the CLI flags re-entering the prior conversation
without re-paying the full setup cost. Adapters that have not opted
in fall through to the normal failure-handling path.

Hard contract:

- Retry is capped at exactly **one** attempt per task. Recursion is
  not configurable.
- Retry only fires when both pre-spawn and post-exit SHAs are read
  successfully. An unknown HEAD (no repo, no commits yet) leaves the
  exit unchanged.
- The lifecycle event `agent.retry_continuation` fires once per
  retry launch with `{session_id, reason, attempt}` for downstream
  observability.

Source: `src/bernstein/core/orchestration/commit_completion.py`.

## Limitations

- The adaptive concurrency layer reacts to recent task outcomes
  and host CPU. A run that bottlenecks on disk I/O or a remote
  service is not detected by these signals.
- Deadline enforcement runs on the tick boundary. A task that
  blocks the orchestrator's tick loop itself bypasses the check
  for the duration of the block.
- The budget guard cannot retroactively refund spend that the
  routed model has already billed; once a request is in flight
  the cost is sunk.

## Related

- Source: `src/bernstein/core/orchestration/orchestrator.py`
- Adaptive parallelism: `src/bernstein/core/orchestration/adaptive_parallelism.py`
- Tick budget: `src/bernstein/core/orchestration/tick_budget.py`
- Cost guard: `src/bernstein/core/cost/cost_tracker.py`
- Commit-completion check: `src/bernstein/core/orchestration/commit_completion.py`
- [Adaptive parallelism](../architecture/adaptive-parallelism.md)
- [Cost optimisation](../operations/cost-optimization.md)
