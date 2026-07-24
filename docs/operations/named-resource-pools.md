# Named resource pools

Lease-backed admission control - concurrency-limited pools, per-tag ceilings,
adaptive rate limits, and priority queues - where a grant's identity is the
hash-chained ledger row that issued it.

```
bernstein limits pool create <name> --slots N
bernstein limits tag set <tag> --limit N
bernstein limits rate set <name> --base-limit N
bernstein limits queue create <name>
bernstein limits status
bernstein limits verify
```

Not to be confused with **named sandbox pools** (`bernstein pool
register/list/show/verify`, `core/sandbox/pool.py`) - that is a separate
subsystem governing which sandbox backend and workspace template a run may
place work into. `bernstein limits` governs *how many* tasks may run
concurrently against a named resource, a tag, or an external rate limit.

## Why

Concurrency and rate limits are usually enforced ad hoc - a hardcoded
semaphore, an environment variable nobody remembers changing. The admission
subsystem treats every limit and every grant as a chained fact instead:
pool occupancy, queue order, effective rate limits, and enforcement postures
are never a mutable side table, they are a pure projection of a hash-chained
ledger mirrored into the HMAC audit chain. Two operators replaying the same
ledger derive the byte-identical grant order, so slot forensics ("why did
task X get admitted ahead of task Y") is a replay, not an investigation.

## Concepts

| Concept | What it limits |
|---|---|
| **Pool** | Named concurrency ceiling for a resource (`--slots N`), e.g. one staging environment or a migration lock. |
| **Tag limit** | Concurrency ceiling over any task carrying a given tag (`--limit N`; `0` quarantines the class). |
| **Rate limit** | Fleet-wide named rate limit with adaptive decay driven by recorded 429 observations (`--base-limit`, `--floor`). |
| **Queue** | Operator-defined named queue generalizing the deterministic round-robin (DRR) scheduler, with a priority and pause/resume. |
| **Posture** | `enforce` (block over-limit grants), `advise` (admit over-limit but issue a signed waiver receipt), or `off` (inert). Default `enforce`. |
| **Grant** | The admission decision for one task; its identity is the `entry_hash` of the ledger row that issued it - never a separately minted id. |

## Commands

### `bernstein limits pool create <name> --slots N`

Create or update a named slot pool. `--slots 0` quarantines the pool (no new
grants admitted). `--posture` sets enforcement posture (default `enforce`).

### `bernstein limits tag set <tag> --limit N`

Set a concurrency ceiling over a task tag. `--limit 0` quarantines the tag.

### `bernstein limits rate set <name> --base-limit N [--floor N]`

Define a fleet-wide named rate limit. `--base-limit` is the ceiling with zero
recent 429 observations; `--floor` (default `1`) is the lowest the adaptive
limit may decay to as 429s are observed.

### `bernstein limits queue create <name> [--priority N]`

Create or update an operator-defined named queue (default priority `0`,
higher runs first; aging lifts starved queues).

### `bernstein limits queue pause <name> [--resume]`

Pause a named queue; pass `--resume` to resume it instead. Refuses (rather
than silently creating) an unknown queue name.

### `bernstein limits status`

Show the projected admission state: pools (slots / held / posture), tag
limits (limit / held / posture), active grant count, waiver count, and
quarantine count. `--json` emits the full canonical projection.

### `bernstein limits verify`

Recompute the admission ledger from genesis and fail closed on any drift.

```
Exit codes:
  0  the admission ledger verifies end to end
  2  verification failed (the exact position is named)
```

All commands accept `--workdir PATH` (defaults to the current directory) and
`--json` for machine-readable output.

## How a grant is decided

Every declaration (`pool create`, `tag set`, `rate set`, `queue create`) and
every grant, release, renewal, expiry, waiver, and quarantine is appended as
a row to a hash-chained ledger under `.sdd/runtime/admission/<ledger-id>/`
(default ledger id `fleet`) - the same hashing contract the work ledger uses,
so mutating or reordering a row surfaces as a hash mismatch at an exact
position. `AdmissionEngine.request_grant()` reads the current projected
state, evaluates the candidate against the pool/tag gates:

- **Under `enforce`**, a full pool or tag refuses the grant outright (the
  task waits); the refusal reason names which gate was over capacity.
- **Under `advise`**, the grant is admitted `over_limit=True` and paired with
  a signed waiver receipt naming exactly the gate(s) exceeded.
- **Under `off`**, the gate is inert.

## Lease expiry and quarantine

A grant carries an optional TTL (`ttl_s`); a lease past its TTL is expired by
a deterministic sweep (`sweep_expired()`), never silently recycled. Each
expiry appends an `admission.expire` row, assembles a signed escalation
receipt in the same shape [stall escalation](stall-escalation.md) uses, and
computes a checkpointed-retry resume decision so a warm resume is honoured
when a checkpoint exists.

`quarantine(target_kind="pool"|"tag", target=...)` freezes a class in one
operation: it sets the pool or tag's limit to `0`, expires every in-flight
matching grant (checkpointing its worker through the same expiry lifecycle),
and emits **one** chain entry carrying the complete affected-set manifest -
the checkpointed workers plus any queued task ids. Replaying the chain
reproduces the identical manifest, because the manifest is the hashed row
payload, not a bare relabel.

## Limitations

- There is no per-pool dashboard beyond `bernstein limits status`; historical
  occupancy over time requires reading the ledger directly.
- Rate-limit decay is adaptive over recorded 429s but the decay curve itself
  is not operator-tunable beyond `--base-limit` / `--floor`.

## Source

- `src/bernstein/core/admission/__init__.py` - subsystem overview and public
  surface.
- `src/bernstein/core/admission/engine.py` - `AdmissionEngine` (grants,
  leases, quarantine, waivers).
- `src/bernstein/core/admission/ledger.py` - the hash-chained ledger storage.
- `src/bernstein/core/admission/projection.py` - `AdmissionState`, the pure
  projection.
- `src/bernstein/core/admission/verify.py` - `verify_admission_ledger()`.
- `src/bernstein/cli/commands/limits_cmd.py` - the `bernstein limits`
  command group.
