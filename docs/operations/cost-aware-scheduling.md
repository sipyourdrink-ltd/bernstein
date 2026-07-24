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
rather than corrupting every downstream budget decision. A partial override that
names only `input`/`output` inherits the base row's `cache_read`/`cache_write`
rather than zeroing them, so re-rating a model's list price never silently
understates its cache spend; name a cache rate explicitly (including `0.0`) to
change it. `bernstein doctor` prints a staleness advisory when the shipped
table's `as_of` date is older than the staleness window (default 90 days), a
reminder that provider rates drift between releases.

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

### Live enforcement in the run loop

The ceilings are enforced inside the live `bernstein run` spawn loop, not only
in preflight. On every tick the orchestrator costs the batches it is about to
spawn from their pre-spawn estimates and consults the policy **before** any
agent is claimed or spawned. When the projected next dispatch would breach a
cap, the tick holds every spawn and seals the halt receipt described above, so
the run stops **before** the cap is breached rather than discovering the
overrun after the fact.

The gate is conservative at both ends:

- **Fail-open on config-absent.** With no `cost_policy` block -- or with every
  cap left at `0` (unlimited) -- the gate is a clean no-op and never touches the
  ledger, so a run with no cost policy behaves exactly as before.
- **Fail-closed on breach.** With a positive cap configured, a projected breach
  halts the tick. Candidates are evaluated in dispatch order and the spend a
  candidate would commit is folded into the projection for the candidates behind
  it, so a batch of small dispatches that only *together* breach still halts on
  the exact candidate that tips a dimension over.

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

### Live routing in the run loop

The routing decision is consulted inside the live spawn loop, not only in
preflight. Before a single-task batch is dispatched, the run resolves the
adapter that would run the task (a per-role adapter pin wins, else the active
default adapter), derives batch eligibility from the same classifier the
provider-batch path uses, and takes the capability-gated route:

- a batch-eligible task on a batch-capable adapter is submitted to the batch
  surface (no local agent is spawned);
- a batch-eligible task on an adapter with no batch surface is refused and
  dispatched interactively;
- a task that is not batch-eligible bypasses the batch surface entirely.

Each routing decision is sealed as a `cost.batch_route` audit-chain event, so
the routing of a task -- to the batch endpoint or to interactive dispatch -- is
an independently verifiable receipt rather than a log line. A verifier holding
the chain, the adapter capability map, and the task's eligibility recomputes the
same verdict.

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

The fan-out is wired into the tournament runner, whose sibling attempts of one
task share the same prompt prefix -- the exact shape a prompt cache rewards.
When the resolved adapter is cache-window capable and the opt-in is set, the
runner issues one warm-up call to prime the shared prefix strictly before the
siblings spawn, so each sibling hits the warm cache inside the TTL instead of
racing to write it. The observed warm-up and cache-hit counts are recorded on
the round outcome.

## Dispatch knob matrix

The price table is keyed by model name, but the per-call knobs that actually
move the price of an identical task are the reasoning **effort** level, the
processing **lane** (interactive / priority / batch, each with its own USD rate
multiplier), and the prompt-cache **strategy** (none / reuse / warm-up). The
**knob matrix** pins those, model by model, as a versioned, content-addressed
map -- its `sha256` content hash pins exactly which knob economics a dispatch
resolved against. Its defaults derive from the same sources the rest of the
layer uses: the cache economics come from the price rows (a model that prices
cache reads offers `reuse`; one that prices cache writes offers `warm_up`), and
the lanes exist because the adapter contract declares a batch surface.

Inspect the pinned matrix and its hash:

```bash
bernstein cost policy knobs                 # full matrix + content hash
bernstein cost policy knobs --model opus    # one row
```

Operators override the shipped defaults without a code change, exactly like the
price table:

```yaml
cost_policy:
  knobs:
    as_of: "2026-07-01"
    revision: 2
    models:
      opus:
        effort_levels: ["low", "high"]
        default_effort: "low"
        lanes: { interactive: 1.0, batch: 0.5 }
```

For each dispatch a **pure resolver** (no clock, filesystem, or network) turns
the candidate plus the pinned matrix into a sealed **knob selection**: the
resolved effort, lane, cache strategy, and rate multiplier, each carrying its
own digest. The selection's content hash **folds into the `decision_hash`**, so
two operators with identical state provably dispatch identically (byte-identical
fingerprint) and a knob change surfaces as fingerprint divergence rather than an
unexplained cost delta. The candidate projection applies the resolved lane
multiplier, so admit and halt decisions account for the lane and effort actually
chosen.

The halt receipt seals the selection alongside `price_table_hash`,
`ledger_state_hash`, and `policy_hash`. `bernstein cost policy verify` re-derives
it offline: mutating any single knob field (effort, lane, cache strategy, or
multiplier) fails verification with the mismatching field **named**, because the
selection is falsification-evident per field, not merely logged. A model absent
from the matrix resolves to an explicit default carrying `resolved=false` and a
machine-readable reason with multiplier `1.0`, so the admit/halt outcome is
unchanged for unpriced models -- never a silent fallback.

The resolved selection is also recorded as a Merkle-chained event in the run
journal, so replaying a run with a different knob assignment is reported as
divergence at the exact step index. `bernstein doctor` carries a non-blocking
advisory when the shipped matrix is stale.

## Determinism and verifiability

The whole layer is a deterministic projection. The decision reads no clock, no
filesystem, and no network; the ledger projection, the cap comparison, and
every hash are pure functions of the inputs. That is what makes a halt
reproducible and a receipt independently verifiable offline -- the audit chain
in the shape of a budget decision, not a log line beside it.
