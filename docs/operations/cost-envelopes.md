# Cost envelopes

A **quota envelope** tags spend against a named budget bucket - for
example `subscription` (the default) versus a metered API pool - so
operators can cap and report spend per bucket instead of only in
aggregate. Every recorded LLM call carries a `quota_envelope` tag; the
rollup in `core/cost/cost_rollup_by_envelope.py` aggregates the ledger by
that tag into per-envelope spend, cap, and burn-rate reports.

Envelopes are distinct from the [task-budgets countdown](../concepts/task-budgets.md)
(the per-turn token/dollar/step banner shown to the model) and from
[cost-aware scheduling](cost-aware-scheduling.md) (the pre-dispatch price-table
policy layer). Envelopes answer "how much has bucket X spent, and against
what cap", after the fact or live from the ledger - no scheduling decision
depends on them.

## Configuring envelopes

Declare envelopes under `cost.envelopes` in `bernstein.yaml`:

```yaml
cost:
  envelopes:
    subscription:
      budget_usd: 50.0        # soft cap; 0 = unlimited
      hard_budget_usd: 0.0    # hard cap; 0 = unlimited
      threshold_pct: 0.8      # default: 0.80
      model_allowlist: []     # empty = any model permitted
    ci-metered:
      budget_usd: 20.0
      hard_budget_usd: 25.0
      model_allowlist: ["haiku"]
```

Calls tag their envelope by passing `quota_envelope=` to
`CostTracker.record()` (default: `"subscription"`, `DEFAULT_QUOTA_ENVELOPE`).
An envelope observed in the ledger but absent from `cost.envelopes` still
appears in reports as an uncapped bucket.

## Viewing the rollup

```
bernstein cost-envelopes show [--ledger PATH] [--config PATH] [--last 1h|24h|7d|30d] [--json]
```

| Flag | Default | Meaning |
|---|---|---|
| `--ledger` | `.sdd/cost/ledger.jsonl` | Rolling spend ledger to read. |
| `--config` | `bernstein.yaml` | File holding the `cost.envelopes` block. |
| `--last` | none | Restrict to a time window (`1h`, `24h`, `7d`, `30d`). |
| `--json` | off | Emit raw JSON instead of the Rich table. |

The table shows, per envelope: spent, cap, percent used, hard cap, call
count, and a status column (`ok`, `threshold`, or `HARD BREACH`).

Per-envelope spend is also available as a grouping dimension on the
general cost report:

```
bernstein cost --by envelope
```

## How it behaves

- **Rollup is a pure aggregation.** `cost_rollup_by_envelope.rollup()`
  takes a list of `TokenUsage` records and an optional envelope-config
  mapping and returns one `EnvelopeRollupRow` per envelope - it does not
  read the ledger or config itself; the CLI command wires those in.
- **Soft cap (`budget_usd`)** drives `pct_used` and `threshold_reached`
  (fires once `pct_used >= threshold_pct`, default 80%). It is advisory:
  exceeding it does not block anything by itself.
- **Hard cap (`hard_budget_usd`)** is enforced live, at record time, by
  `CostTracker.record()` - not by the rollup. A call that would push an
  envelope's cumulative spend past its hard cap raises
  `EnvelopeBudgetError` before the call is recorded. A model outside an
  envelope's `model_allowlist` is refused the same way.
  `cost_rollup_by_envelope` separately reports `hard_breached=True` for
  envelopes already over their hard cap in the ledger, for
  reporting/dashboard purposes.
- **Forecast-to-cap** is a linear extrapolation (`remaining_usd /
  burn_rate_usd_per_sec`) using the burn rate observed across the
  ledger records in view. It returns `None` (rendered as `"--"`) when
  fewer than two records exist or the observed window collapses to a
  point - the rollup never prints a projection it cannot support.
- **Unconfigured envelopes are never dropped.** An envelope name seen in
  the ledger but missing from `cost.envelopes` still gets a rollup row
  (`cap=0.0`, i.e. unlimited), so a config gap doesn't silently hide
  spend.

## Source

- Rollup: `src/bernstein/core/cost/cost_rollup_by_envelope.py`
- Envelope config + live hard-cap enforcement: `src/bernstein/core/cost/cost_tracker.py` (`EnvelopeConfig`, `CostTracker.record`)
- CLI: `src/bernstein/cli/commands/cost.py` (`cost_envelopes_group`)
