# Budget halt receipts

When a run's spend ledger crosses its soft or hard cap, the halt is
recorded in the tamper-evident audit chain as a `cost.budget_halt` event,
not only as a warning on stderr. "This run stopped because of its budget"
is then answerable from the chain alone, after the process is gone and
after the logs have rotated.

## What is recorded

`SpendLedger` marks each cap transition exactly once. On the first soft
halt and on the first hard halt it appends one event:

| Field | Meaning |
|---|---|
| `run_id` | Run whose ledger tripped the cap |
| `band` | `soft` or `hard` - the closed set of caps that can halt a run |
| `spent_nano_usd` | Cumulative spend at the halt, integer nano-USD |
| `cap_nano_usd` | The cap that was crossed, integer nano-USD |
| `ledger_entries_written` | Rows this ledger instance had appended when the cap tripped |
| `prev_chain_digest` | Chain head at write time, embedded before the HMAC is computed |

Amounts are integer nano-USD via
[`nano_usd_from_float`](showback-canonical.md) - the same fixed-scale
money policy showback statements use. No float reaches the recorded
payload, so two readers summing the same events get the same digits.

`ledger_entries_written` locates the boundary row: the halt fired on the
row at that index in `.sdd/cost/ledger.jsonl`, so an operator can point at
the exact call that crossed the line.

## Attaching a chain

The chain is optional. A ledger constructed without one behaves exactly
as it did before - the caps still trip, the warnings still print, and no
audit directory is created:

```python
from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.security.audit_chain import AuditChainStore

ledger = SpendLedger(
    path=project / ".sdd" / "cost" / "ledger.jsonl",
    run_id=run_id,
    hard_budget_usd=25.0,
    chain=AuditChainStore(project / ".sdd" / "audit"),
)
```

Appending the receipt is best-effort in the same sense as the JSONL write:
a chain that refuses the append is logged as a warning and never takes the
orchestrator down. The ledger is not a gate.

## Reading the halts back

```python
from bernstein.core.security.audit_chain import EVENT_BUDGET_HALT

ok, errors = chain.verify()
halts = chain.query(event_type=EVENT_BUDGET_HALT, resource_id=run_id)
```

Verify first, then project: an edit to a recorded cap, band, or spend
figure breaks the HMAC of its entry and every entry after it, so
`verify()` returns `False` rather than handing back an amount somebody
adjusted afterwards.

## Naming

"Envelope" already means a named budget *bucket tag* in this subsystem
(`CallTags.quota_envelope`, `cost_rollup_by_envelope`,
[cost envelopes](../operations/cost-envelopes.md)). A halt receipt is a
different thing: the record of one cap transition on one run, not a
spending bucket.
