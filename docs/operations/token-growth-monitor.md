# Token growth monitor

Every orchestrator tick, Bernstein reads each live agent's token sidecar
file, tracks its cumulative consumption, and intervenes when an agent is
burning tokens without producing output or when its context is growing
super-linearly. Interventions range from a one-time "wrap up" nudge to a
hard kill.

Source: `src/bernstein/core/tokens/token_monitor.py`, called each tick as
`check_token_growth(orch)` from `core/orchestration/orchestrator.py`.

## How it reads usage

Each agent session writes token records to
`.sdd/runtime/{session_id}.tokens` - one JSON line per model turn,
`{"ts": <float>, "in": <int>, "out": <int>}`. The monitor reads only the
bytes appended since its last read (a stored byte offset), so polling every
tick is cheap. The returned usage total includes the input and output counts
read from those records; reading usage and recording a history sample are
separate steps.

When a read finds appended bytes, it adds a `(timestamp, cumulative_tokens)`
history sample only if the history is empty or at least 30 seconds
(`TOKEN.sample_interval_s`) have passed since the last sample. This is a
minimum spacing, not a timer: a tick with no appended bytes adds no sample,
even after 30 seconds. History retains at most 20 entries, not a fixed
ten-minute window. With exactly 30 seconds between entries, the oldest and
newest of 20 samples are 570 seconds apart; idle periods or delayed ticks
can make that span longer.

## Interventions, in the order the tick hook applies them

| # | Check | Trigger | Action |
|---|---|---|---|
| 1 | Auto-kill | cumulative tokens ≥ 50,000 (`TOKEN.kill_threshold`) **and** zero files changed | `SIGKILL` the agent, transition it to `dead` (reason `token budget exceeded`) |
| 2 | Quadratic-growth warning | the most recent per-window token delta is ≥ 2x (`TOKEN.quadratic_ratio`) the previous delta, with ≥3 samples | log a warning once, send a WAKEUP signal to the agent |
| 3 | Proactive compaction | delegates to the `compaction.proactive` lane (off by default) | see [Proactive context compaction](context-compaction.md) |
| 4 | Context-window compaction | context utilization crosses 90% (`TOKEN.compact_threshold_pct`) and the per-session circuit breaker allows it | send a compaction WAKEUP signal |
| 5 | Budget nudge | `tokens_used` reaches 80% of the session's configured `token_budget` | send a one-time WAKEUP asking the agent to wrap up |

"Files changed" for the auto-kill check comes from the latest progress
snapshot for each of the agent's tasks. If no snapshot exists yet, the check
is skipped for that tick (conservative - an agent is never killed before
there is any evidence about its output).

### Auto-kill

```
Token runaway: agent <id> consumed <N> tokens with 0 file changes
(tenant=<tenant> threshold=<threshold>) - killing
```

The kill threshold can be overridden per tenant via
`TokenGrowthMonitor.tenant_kill_thresholds` (a `{tenant_id: threshold}` map)
or the module-level `TOKEN_CFG` dict - both are set programmatically, not
through `bernstein.yaml`; there is currently no config-file surface for
per-tenant overrides.

### Quadratic-growth warning

The check uses the last three stored cumulative totals, `T1`, `T2`, and
`T3`. It compares the two consecutive deltas `T2 - T1` and `T3 - T2`,
warning when the newer delta is at least `quadratic_ratio` (2.0x) the older
one. The earlier delta must be positive: if it is zero or negative, the
check returns false, even if the newer total jumps. These are token-count
deltas, not rates normalized by elapsed time.

For example, totals of 100, 200, and 500 give deltas of 100 and 300.
Since 300 is at least twice 100, this triggers growth detection with the
default ratio. Totals of 100, 100, and 500 do not, because the earlier delta
is zero.

An active warning suppresses further warnings. Its reset counter advances
on each non-growth observation and clears the warning at 10, allowing later
growth to warn again. Ten consecutive non-growth observations therefore
suffice, but growth ticks during an active warning leave the counter
unchanged, so the observations need not be uninterrupted. Each observation
is an orchestrator tick reaching this check and finding no quadratic growth;
it does not require a new history sample. Ticks can recheck the same history,
so the reset does not promise ten sampling intervals or a fixed elapsed time.

### Budget nudge

The nudge text instructs the agent to finish its current edit, run tests on
changed files, commit with a `[WIP]` message, mark the task complete (or
failed), and exit cleanly. It fires at most once per session.

### Auto-compact circuit breaker

The context-window compaction trigger (row 4 above) is gated by a per-session
circuit breaker (`AutoCompactCircuitBreaker`): `CLOSED` → `OPEN` after 3
consecutive compaction failures (`TOKEN.compact_max_failures`), `OPEN` →
`HALF_OPEN` after a 120s cooldown (`TOKEN.compact_cooldown_s`), and back to
`CLOSED` on the next success. This is the *reactive* compaction trigger -
distinct from the separately-configured, receipted *proactive* lane described
in [Proactive context compaction](context-compaction.md).

## Defaults

All thresholds live in `TokenDefaults` (`src/bernstein/core/defaults.py`):

| Constant | Default | Meaning |
|---|---|---|
| `kill_threshold` | 50,000 | tokens before auto-kill (with zero file changes) |
| `min_samples_for_growth_check` | 3 | samples needed before quadratic check runs |
| `quadratic_ratio` | 2.0 | growth-rate multiplier that triggers the warning |
| `sample_interval_s` | 30.0 | minimum seconds between recorded samples |
| `compact_threshold_pct` | 90.0 | context-window % that triggers compaction |
| `compact_max_failures` | 3 | consecutive compaction failures before the breaker opens |
| `compact_cooldown_s` | 120.0 | seconds before a retry after the breaker opens |
| `nudge_threshold_pct` | 80.0 | fraction of `token_budget` that triggers the wrap-up nudge |

## Limitations

- The monitor is unconditional - there is no `enabled` flag in
  `bernstein.yaml` to turn it off. Overriding thresholds requires
  constructing a `TokenGrowthMonitor` with different values or populating
  `TOKEN_CFG` / `tenant_kill_thresholds` programmatically.
- The auto-kill check treats "zero file changes" as the sole progress
  signal; an agent doing legitimate long-running analysis with no file
  writes yet (e.g. an investigation task) can hit the kill threshold.

## Related

- [Proactive context compaction](context-compaction.md) - the separately
  configured, receipted compaction lane this monitor's reactive trigger
  complements.
