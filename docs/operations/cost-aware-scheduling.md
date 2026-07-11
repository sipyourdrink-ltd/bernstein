# Cost-aware scheduling

Model spend is the dominant operating cost of a fleet, and its structure has
shifted: token-denominated budgets break when providers change tokenizers or
models, programmatic usage is metered in credit pools separate from interactive
subscription quotas, batch endpoints discount non-interactive work, and prompt
cache hits are far cheaper but expire on short TTLs. The scheduler has per-role
model policy and per-task budget flags, but no cost model actually driving
scheduling decisions -- so overruns are discovered after the fact.

Cost-aware scheduling (issue #2354) adds a **deterministic cost policy layer**.
Every decision is a pure function of a hash-pinned price table, the existing
spend ledger, and the policy config, so two operators with the same ledger
reproduce identical scheduling decisions and can audit why any dispatch
happened. Each budget decision is a journal-anchored receipt.

## The price table

USD ceilings are enforced against a versioned, content-addressed price table --
not a network lookup inside the scheduling loop. The shipped default is sourced
from the same list prices the ledger already meters against. Override it in
`bernstein.yaml` when a provider re-rates a model:

```yaml
cost_policy:
  pricing:
    as_of: "2026-07-01"     # ISO date the rates were captured
    revision: 3             # monotonic; bump on any rate change
    models:
      sonnet:
        input: 3.0          # USD per 1M input tokens
        output: 15.0        # USD per 1M output tokens
        cache_read: 0.3
        cache_write: 3.75
```

Rates are schema-validated non-negative; a negative rate fails at load time
rather than corrupting every downstream budget decision. `bernstein doctor`
prints a staleness advisory when the shipped table's `as_of` date is older than
the staleness window (default 90 days), a reminder that provider rates drift
between releases.

## USD ceilings and the halt receipt

Configure per-task, per-run, and per-day USD ceilings (`0` means unlimited):

```yaml
cost_policy:
  caps:
    per_task_usd: 5.0
    per_run_usd: 20.0
    per_day_usd: 100.0
```

Before dispatch, the policy projects prior spend from the ledger, adds the
candidate's projected cost, and compares each capped dimension (task, then run,
then day). When a ceiling would be exceeded the dispatch **halts**, and the halt
is a sealed receipt naming exactly why it fired:

- the pinned `price_table_hash` the candidate was priced against,
- the `ledger_state_hash` over the projected prior spend,
- the `policy_hash` over the caps, and
- the `breached_dimension` and the `projected_overrun_usd`.

The decision's canonical bytes are anchored in the `cost-dispatch` run of the
Merkle+HMAC lineage spine, and the receipt identity is mirrored into the audit
chain (`cost.dispatch_receipt`). The receipt **is** the proof: a verifier
holding the same ledger and price table recomputes the `decision_hash`
byte-identically, so two operators replay the same budget decision. Verify one
offline:

```bash
bernstein cost policy verify <decision_hash> --workdir .
```

A forged receipt (an `admit` flipped from `false` to `true`, an overrun zeroed)
recomputes to a different decision hash and fails verification exactly like a
tampered chain entry.

## Pool accounting and pre-run exhaustion

Usage is attributed to named pools (the ledger's `quota_envelope` column, e.g.
`api`, `subscription`) with independent caps. Pool exhaustion is surfaced
**before a run starts**, not mid-run:

```yaml
cost_policy:
  pools:
    api: 50.0            # USD cap; 0 = unlimited
    subscription: 0.0
```

```bash
bernstein cost policy preflight --plan "api=2.50,subscription=0"
```

The preflight projects the ledger into pools, adds the planned run spend, and
exits non-zero when any capped pool is (or would be) exhausted -- so exhaustion
stops a run at the gate. A pool whose ledger spend alone already meets its cap
is flagged as already exhausted.

## Batch dispatch (capability-gated)

Batch endpoints discount non-interactive work, but only some providers expose
one. Batch routing is gated on a declared adapter capability map -- the single
source of truth -- so a batch-eligible task reaches a batch endpoint **only**
when the adapter actually has one. An eligible task on a non-batch adapter is
*refused* (routed interactively with a recorded reason), never faked onto a
batch path that does not exist. A task that is not batch-eligible never routes
to batch.

## Cache-window fan-out (capability-gated, default off)

When M workers share a prompt prefix, dispatching them concurrently makes them
race to write the prompt cache M times. Cost-aware scheduling can issue one
**warm-up** call first to prime the cache so the M workers all hit it inside the
TTL window. Two guards keep this safe:

- **Capability-gated.** Only adapters whose upstream documents a prompt-cache
  window are eligible (declared, never probed).
- **Conservative default off.** Even a capable adapter needs an explicit opt-in;
  with the default off the fan-out issues no warm-up and assumes no hits.

```yaml
cost_policy:
  cache_window: true    # opt in; default is false
```

With the opt-in on a capable adapter, a fan-out of M workers issues one warm-up
call plus M cache-hitting calls. With the default off, the workers race without
a warm-up and no hits are assumed.

## Determinism and verifiability

The whole layer is a deterministic projection. The decision reads no clock, no
filesystem, and no network; the ledger projection, the cap comparison, and
every hash are pure functions of the inputs. That is what makes a halt
reproducible and a receipt independently verifiable offline -- the audit chain
in the shape of a budget decision, not a log line beside it.
