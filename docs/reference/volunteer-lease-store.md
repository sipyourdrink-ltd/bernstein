# Volunteer lease store

`LeaseStore` hands one task to exactly one enrolled donor worker for a bounded
time, and hands it to somebody else if that worker goes dark.

It sits beside the [task runner](volunteer-runner.md)'s claim etiquette without
touching it, and the seam is deliberate. Etiquette decides whether a claim is
*legitimate* — a marker in an issue comment, scoped to its author, honoured for
a staleness window. This decides where a claim *survives a process death*.
Nothing here re-derives etiquette and nothing there knows how a lease is
persisted; the two windows answer different questions and must not be collapsed
into one.

Nothing in the codebase did this before. `TaskStore.claim_next` claims a task
permanently — a worker that dies holding one holds it until an operator
intervenes. `POST /cluster/steal` reassigns work, but it is admin-triggered
load balancing between trusted nodes, with no lease, no TTL, and nothing that
fires on its own when a holder stops answering. A fleet of strangers' laptops
needs the third thing: a hold that lapses.

## The operations

Every mutating operation is `async`, takes the store's lock, and reaps expired
leases as its first statement inside that lock. Reads (`lease_for`,
`is_enrolled`) are synchronous and take no lock.

| Operation | What it does | How it refuses |
|---|---|---|
| `enroll` | Registers a worker's Ed25519 public key and returns its key id (`sha256` of the SPKI DER). Idempotent by construction — the id *is* the key, so re-enrolling returns the same id and appends nothing. | Never refuses |
| `claim` | Grants a lease for `ttl_seconds`, numbered one higher than the last hold on that task however it ended (see [generations](#generations-count-holds-not-leases)). Same worker re-claiming its own live lease → re-grant with an extended expiry, same generation. | `unknown_worker`, `already_leased`, `already_submitted` |
| `heartbeat` | Extends the lease to `now + ttl_seconds` and stamps `heartbeat_at`. The TTL from the original claim is stored on the `Lease`, so a heartbeat cannot quietly change the loan's length. | `no_lease`, `lease_reassigned`, `not_lease_holder` |
| `release` | Drops the lease. The task is immediately claimable by anyone, including the worker that just let it go. | `no_lease`, `not_lease_holder` |
| `submit` | Attaches a `Submission` (`bundle_digest`, `location`, `submitted_at`) to the lease. The lease becomes terminal. | `no_lease`, `lease_reassigned`, `not_lease_holder`, `already_submitted` |
| `reap_expired` | Drops every expired, unsubmitted lease and returns one `ReassignedLease` per drop, in `task_id` sort order. | Never refuses; returns an empty tuple |

Nothing here raises at the caller. A refusal is a value — `LeaseRefusal`, a
reason code and a detail string — for the same reason the runner's refusals
are: on a fleet of donor machines a refusal is the ordinary outcome, not the
exceptional one, and a refusal that arrives as an exception is one somebody has
to guess how to catch. `TaskStore` raises; this package does not.

## Refusal reasons

| Reason | Meaning |
|---|---|
| `already_leased` | A live lease on this task is held by another worker |
| `already_submitted` | Terminal: a submission exists for this task |
| `not_lease_holder` | A lease exists and the caller is not its holder |
| `lease_reassigned` | The caller's lease expired and was reaped; someone else holds the task now |
| `no_lease` | There is no lease on this task at all |
| `unknown_worker` | The `worker_id` was never enrolled |

`lease_reassigned` and `not_lease_holder` both mean "you are not the holder",
and they are two codes on purpose. The first is a worker discovering that *it
lost the task* — its own machine slept, its network dropped, the hold lapsed —
and the right response is to stop working and drop the diff on the floor. The
second is a worker addressing a task it never held, which is a bug or a
misrouted request. Collapsing them would make the fleet's most interesting
failure mode indistinguishable from a typo.

## This store is single-process. Do not run it under more than one worker.

The concurrency model is the one `TaskStore` documents at
`src/bernstein/core/tasks/task_store_core.py:432-451`, deliberately copied
rather than improved on: mutations are serialised by an in-process
`asyncio.Lock`, and the JSONL append path takes **no** OS-level file lock — no
`fcntl.flock`, no SQLite WAL, no external coordinator.

So the guarantee this module offers is exactly "one worker at a time" **within
one process**, and it does not survive being run under `uvicorn --workers N` or
with `WEB_CONCURRENCY>1`. Two processes each replay the same file into their
own dictionary, each takes its own lock, and each believes it holds the only
copy of a lease. Both grant a claim on the same task to two different donors,
both append to the same file, and the interleaved appends produce torn lines
that replay silently drops. Every property on this page fails at once: the
single-holder invariant, the generation counter, and exactly-once submission.

That failure is silent, which is why it is stated here in full rather than
noted as a limitation at the bottom. The store is not degraded under multiple
processes; it is wrong, and it looks fine.

The hub's `serve` command must therefore refuse to start with more than one
worker process, the way
`preflight_multi_worker_guard` (`src/bernstein/core/server/server_app.py:933`)
already does for the main server — resolving `BERNSTEIN_WORKERS` /
`WEB_CONCURRENCY` and exiting with a message naming the single supported
configuration, at app-factory time so each worker subprocess re-runs it on
import and bails out instead of corrupting shared state. Multi-process
coordination is a change to the storage layer, not a flag.

## Expiry is checked inside the lock, not by a background reaper

A background reaper is the obvious design and it buys nothing here.

It needs lifespan wiring — a task started at startup, cancelled at shutdown,
kept alive across reloads — which is real surface for a store that is otherwise
a file and a dictionary. Worse, it races: a reaper deciding a lease is expired
while a `heartbeat` extends it is exactly the interleaving that reassigns a
task out from under a worker that was answering on time. The fix for that race
is to make the reaper take the store's lock, at which point it is doing, on a
timer, the work every mutation could do for free.

So expiry is checked as the first statement inside the lock that every mutation
already takes. The race is removed by construction rather than by ordering
arguments: no mutation can observe a lease that expired before it, because
looking is the first thing it does. `reap_expired` remains public as a thin
wrapper over the same internal call, for a caller that wants the reassignment
records without also mutating something.

The cost is that a lease expires when someone next looks, not at the instant
the clock passes it. Nothing observable depends on the difference — every read
of a lease goes through an operation that reaps first, so no caller can ever
see an expired lease treated as live.

## A reap writes a record, because a silent one is not exactly-once

Reaping appends a `reassign` record before returning. It would be tempting not
to: the lease was dropped from memory, the next claim will write a fresh lease
at the next generation, and the record looks redundant.

It is not, because replay reconstructs state from the file and the file has to
say what happened. Without the record, a restart replays the original `lease`
record, finds it expired *again*, and reassigns a second time — the task goes
out to a third worker on the strength of one lapse. Exactly-once assignment
would hold within a process and quietly break across a restart, which is the
worst place for it to break, because restarts are how operators fix things.

This is why `release` gets a record too. Replay is last-write-wins in file
order: `worker` registers, `lease` sets, and `release` and `reassign` both pop.
A lease that was ended has to leave a mark saying so, or the file remembers a
hold that no longer exists.

## Generations count holds, not leases

Every lease carries a `generation`, and it counts *holds of that task* — one
higher than the last one, whoever held it and however it ended: reaped,
released, or superseded.

The counter therefore has to outlive the lease it numbered, and it does: it is
rebuilt from the `lease` records on replay rather than read off the live lease,
which by then is gone. Numbering from the live lease instead would hand out
generation 1 twice for a task that was claimed, released, and claimed again —
and a worker holding the first could not tell it had been superseded by the
second, which is the one thing the number exists to say.

## Wall clock, not `time.monotonic()`

`expires_at` is a Unix timestamp. `time.monotonic()` is the better clock for
measuring a duration — it does not jump when NTP steps or the operator changes
the timezone — and it is the wrong one here, because its zero point is
meaningless after a restart. A lease has to outlive the process holding it: the
hub restarts, replays the file, and has to know whether a hold taken four
minutes ago is still good. A monotonic reading from a previous process answers
nothing.

Expiry itself is never stored as a flag. `expires_at` is the stored value and
"expired" is `now >= expires_at`, computed under the lock. A persisted boolean
would be a second source of truth that is stale the moment the clock moves past
it, and replaying it would import a decision made against a clock reading
nobody kept.

`clock` is injectable (`Callable[[], float]`, defaulting to `time.time`) so
tests drive expiry directly instead of sleeping through it.

## `ttl_seconds` is the caller's, and this module holds no policy

`claim` takes a TTL and stores it verbatim. There is no minimum, no maximum, no
grace window, and no reference to a manifest anywhere in this module.

That is a boundary rather than an omission. The interesting TTL is a manifest's
`max_wall_clock_minutes` plus enough grace to cover a worker uploading its
bundle and the hub processing it — and deriving it needs the manifest, which
belongs to the HTTP surface that loaded it, not to a store that has never seen
one. Putting the grace window here would mean either duplicating manifest
loading into the store or having the store apply a default that silently
disagrees with the policy the project committed. The store's job is to hold a
lease for as long as it was told to.

## A submission is terminal

Once a lease carries a submission, `reap_expired` skips it however long it sits
there, and a `claim` against it refuses `already_submitted`.

Submitted work is not abandoned work. A worker that finishes, submits, and then
disconnects has done the whole job; expiring its lease would send an identical
task to a second donor to redo, and the hub would be paying two strangers'
machines for one diff and then choosing between two bundles for no reason. The
lease's expiry answers "is this worker still on it", and after a submission
that question no longer matters.

The consequence, stated plainly: a submitted task is never automatically
reassigned by this module. Rejecting a bad submission and reopening the task is
a decision with a reviewer behind it, and it belongs to whatever surface makes
that call.

## What this module does not do

- **No HTTP surface.** There are no routes, no request models, and no auth. It
  is a library the hub's endpoints call.
- **No `serve` command.** Including the multi-worker guard described above,
  which is named here as a requirement on the command rather than shipped with
  the store.
- **No multi-process store.** See the section above; this is the constraint,
  not a caveat.
- **No protocol-document validation.** `bundle_digest` and `location` are
  opaque strings, stored and returned verbatim. Whether a digest names a real
  bundle, whether the bundle verifies, and whether its gates match the
  [manifest](volunteer-manifest.md) are all questions for the receipt
  verifier — a lease store that half-checked them would be a second, weaker
  verifier that callers might mistake for the real one.
- **No signature checking on operations.** `enroll` records a public key and
  derives a worker id from it; nothing in `claim`, `heartbeat`, `release` or
  `submit` verifies a signature. Authenticating a request is the HTTP layer's
  job, and `is_enrolled` is what it checks against.

## Source

`src/bernstein/core/volunteer/lease_store.py`.
