# Per-principal cost attribution

`bernstein cost` reports spend per run. When agents are principals with
their own grants, the operator question is narrower: *which agent spent
this, under whose grant?* The cost ledger and the grant chain used to be
two records with no join. They now share an attribution tuple.

- `bernstein cost --by principal` - spend grouped by the principal that
  incurred it.
- `bernstein cost --by grant` - spend grouped by the grant that
  authorized it.
- `bernstein cost --by authorizing_identity` - spend grouped by the
  identity that signed that grant.

Source:

- `src/bernstein/core/cost/principal_attribution.py` - the tuple, the
  rollup, and the per-principal ceiling
- `src/bernstein/core/cost/spend_ledger.py` - the ledger columns
- `src/bernstein/core/cost/cost_tracker.py` - threading at record time
- `src/bernstein/cli/commands/cost.py` - the `--by` dimensions

## The attribution tuple

Three fields travel together on every ledger row:

| Field | Meaning |
| --- | --- |
| `principal_id` | The principal that incurred the cost. |
| `grant_id` | The grant that authorized the call. |
| `authorizing_identity` | The issuer that signed that grant. |

`CostTracker.record()` accepts them as keyword arguments and writes them
onto the ledger row. The grant is threaded from the grant already present
at spawn - `attribution_from_grant(receipt, principal_id=...)` reads
`grant_id` and `issuer` straight off the `GrantReceipt` rather than
re-deriving them at record time.

## What counts as attributed

A cost event is attributed only when it names **both** the principal and
the grant. A principal without a grant says who spent but not by whose
authority, so it is not attribution: the row lands in the `unattributed`
bucket and is additionally counted as `partial`, so incomplete wiring is
visible as incomplete rather than as absent.

Consequences an operator can rely on:

- Activity ingested from a runtime we did not schedule usually carries
  neither field and is reported as `unattributed`.
- Ledger rows written before the columns existed deserialise to empty
  strings and stay in `unattributed`. They are never backfilled onto a
  principal.
- Without a ledger, `--by principal` reports everything as
  `unattributed`. A task record cannot say by whose grant a call was
  made, and inventing one would be worse than saying nothing.

Unattributed spend is never silently folded into a named principal's
total.

## Reconciliation is exact

"Grouping by principal reconciles with the run total" holds exactly, not
to within a float epsilon. The rollup accumulates in integer micro-USD
(10^6 units per USD), so bucket sums equal the run total by construction
and re-running the same rollup over the same rows produces identical
numbers. USD floats are produced only for rendering.

```bash
bernstein cost --by principal --json
bernstein cost --by grant --last 7d
```

## Per-principal ceilings

A spend ceiling can be attached to a principal instead of to a run:

```python
from bernstein.core.cost.cost_tracker import CostTracker
from bernstein.core.cost.principal_attribution import PrincipalEnvelope

tracker = CostTracker(
    run_id="r-1",
    principal_envelopes={
        "agent:a": PrincipalEnvelope(principal_id="agent:a", hard_budget_usd=5.0),
    },
)
```

A call that would breach the ceiling raises `PrincipalBudgetError` before
any total moves, so a refused call leaves the ledger exactly where it
was. The attached `PrincipalRefusal` receipt names the principal, its
grant, the authorizing identity, the amount already spent, the amount
attempted, and the cap - an operator never has to guess which agent was
halted.

`hard_budget_usd` of `0` means unlimited, matching `EnvelopeConfig`.
Unattributed spend has no principal to charge and is therefore reported,
not enforced.
