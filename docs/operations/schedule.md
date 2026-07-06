# Recurring schedules (operator-registered, reproducible firing)

Audience: operators who want to register recurring goals or scenarios
without depending on host-level systemd / cron / an external cloud
scheduler, and who need each fire to be reproducible across multiple
hosts.

Issue: #1798.

## Overview

`bernstein schedule` is the in-project surface for registering recurring
goals. Today's trigger pipeline accepts inbound webhooks from a cloud
routine; this surface gives operators the symmetric local capability so
two operators with identical state can prove they fired the byte-identical
graph at the same time `T`.

Key properties:

- **Deterministic projection.** A fire is a pure function of
  `(schedule_id, fire_time, last_state)`; two operators with the same
  inputs land on the byte-identical task graph and the same
  `projection_hash`. `fire_time` is pinned to an integer epoch (a float
  is rejected because sub-second drift forks two hosts), and `last_state`
  is canonicalised before hashing: dict key order, set/frozenset member
  order, and nested-container order do not perturb the digest, and a
  non-portable scalar (`float`/`NaN`/`Infinity`) in the state mapping is
  rejected with a `TypeError` rather than producing a host-dependent
  hash. The genesis sentinel (`last_state=None`) is unchanged.
- **Audit-chain integration.** Each fire appends a `schedule.fire` entry
  to the existing HMAC-chained audit log carrying
  `(schedule_id, fire_time, projection_hash, prev_chain_digest)`. No
  parallel chain.
- **Misfire policy default = skip.** Catch-up is per-schedule, opt-in.
- **No new runtime dep.** Cron evaluation uses an in-tree 5-field parser
  living under `src/bernstein/core/planning/schedule_store.py`.

Source:

- `src/bernstein/core/planning/schedule_store.py` - store + cron validation.
- `src/bernstein/core/orchestration/schedule_projection.py` - the
  deterministic projection function (pure, no wall-clock inside).
- `src/bernstein/core/orchestration/schedule_supervisor.py` - long-running
  supervisor + misfire policy.
- `src/bernstein/core/trigger_sources/schedule.py` - TriggerEvent
  normaliser.
- `src/bernstein/cli/commands/schedule_cmd.py` - CLI verbs.

## Registration

```bash
# Standard daily digest at 09:00 UTC.
bernstein schedule add --cron "0 9 * * *" --goal "Send daily digest"

# Named scenario instead of a free-form goal.
bernstein schedule add --cron "0 0 * * 1" --scenario security-pentest

# Catch-up policy (opt-in, default is skip).
bernstein schedule add --cron "*/15 * * * *" \
  --goal "Refresh dashboard" \
  --misfire-policy catch_up
```

`schedule add` is idempotent: registering the same
`(cron, goal, scenario_id)` triple twice returns the existing schedule
unchanged. The id is a stable hash so configuration-driven seeders can
re-run safely.

JSON output:

```bash
bernstein schedule list --json
bernstein schedule show <id> --json
```

## Restart semantics

The supervisor persists `last_fire_at` to the schedule's JSON file on
every successful fire. A daemon restart resumes from disk:

- **`skip` policy** (default). The supervisor wakes, computes the most
  recent missed fire instant strictly older than `now`, dispatches that
  single fire, and records a counterfactual receipt for every
  intermediate window the operator can replay.
- **`catch_up` policy** (opt-in). The supervisor dispatches one fire per
  missed window up to the catch-up cap (default `16`). The remainder
  fold into a counterfactual receipt.

The catch-up cap exists so a long outage cannot blow the task queue when
an operator opted into catch-up. Increase the cap inside the supervisor
constructor if your workload tolerates a larger burst.

## Audit interaction

Every fire appends a chain entry to `.sdd/audit/<date>.jsonl` with:

- `event_type = "schedule.fire"`,
- `actor = "schedule_supervisor"`,
- `resource_type = "schedule"`,
- `resource_id = <schedule_id>`,
- `details = {schedule_id, fire_time, projection_hash, rev,
  misfire_policy, prev_chain_digest, goal, scenario_id}`.

`goal` and `scenario_id` are recorded in both the chain entry and the
per-fire receipt so the audit verb can re-derive the projection from the
recorded inputs alone, without depending on the live schedule store
(which may have been edited or removed since the fire).

The chain is the production HMAC chain
(`bernstein.core.security.audit.AuditLog`). We do not introduce a
parallel chain.

Verify the recorded fires:

```bash
bernstein schedule audit          # human table with per-fire status
bernstein schedule audit --json   # JSON, for diff-comparing two hosts / CI gates
```

`schedule audit` is a verification verb, not a printout. For every
non-counterfactual receipt under `.sdd/runtime/schedule_receipts/` it:

1. **Re-derives** the projection by re-running `project_schedule_fire`
   from the receipt's persisted inputs
   (`schedule_id, fire_time, goal, scenario_id`, `last_state=None`) under
   the receipt's recorded `rev`, and confirms the recomputed
   `projection_hash` equals the stored one. A receipt whose
   `projection_hash` was edited to a self-consistent but wrong value is
   caught here.
2. **Cross-checks** the receipt against the matching `schedule.fire`
   audit-chain entry: the entry's `projection_hash`, `prev_chain_digest`,
   and HMAC must agree with the receipt. A receipt edited independently
   of the chain (or vice versa) is reported as a mismatch.
3. **Checks linkage**: the receipt-to-receipt
   `prev_chain_digest -> chain_digest` sequence must be unbroken.

The command exits non-zero and names the offending receipt on any
mismatch, so it is safe to run as a CI gate. Counterfactual receipts
carry empty hashes by design and are skipped. A receipt recorded under a
different projection rev than the current in-tree algorithm cannot be
re-derived locally and is reported as `rev-skip` rather than a false
mismatch; the verifier honours `receipt.rev` rather than the current
rev. Two operators comparing the same nightly window still diff the
`(schedule_id, fire_time, projection_hash, rev)` tuple; the per-host
HMAC differs because it includes the wall-clock timestamp baked into the
audit entry, which is intentional for tamper-evidence.

## Lifecycle

Two surfaces:

- **Standalone worker.** `bernstein schedule run` runs the supervisor in
  the foreground. Useful for systemd-style supervision, for `docker run`
  pinned to one host, or for `--once` invocations from a smoke test.
- **Inside `bernstein daemon`.** The daemon ticks the supervisor on
  every loop so operators who already run the orchestrator daemon get
  the schedule subsystem for free.

`bernstein schedule doctor` (and the main `bernstein doctor` runner)
reports:

- supervisor liveness (whether the supervisor has ticked within the
  doctor's liveness window),
- the timestamp of the last fire (across all schedules),
- the timestamp of the next due fire (and the schedule it belongs to).

## Misfire policy summary

| Policy | Default | Effect on missed windows |
|--------|---------|--------------------------|
| `skip` | yes | Single fire at the most recent missed instant; older windows fold into a counterfactual receipt. |
| `catch_up` | no | One fire per missed window up to the cap. Remainder fold into a counterfactual receipt. |

Counterfactual receipts live under `.sdd/runtime/schedule_receipts/` with
a `-counterfactual.json` suffix. They carry the skipped fire timestamps
so the operator can rebuild the missed projections by replaying the
projection function out-of-band.

## Cron expression support

Standard 5-field syntax: `minute hour day month weekday`.

Supported:

- `*`, lists (`a,b`), ranges (`a-b`), and steps (`*/n`, `a-b/n`).
- Named months (`jan`-`dec`) and weekdays (`sun`-`sat`).
- POSIX day-or-weekday union when both fields are restricted.

Not supported (out of scope for #1798; revisit if operators ask):

- Seconds field (6-field form).
- `@reboot` / `@yearly` aliases.
- `?` and `L` extensions.

Cron evaluation runs in UTC. The host timezone is not part of the
deterministic contract; keeping evaluation in UTC means two operators
on different timezones still fire at the same instant.

## Fire as a hash, not a trigger (#2302)

A recurring fire is a pure projection of `(schedule_id, fire_time,
last_state)` onto a canonical task graph with a deterministic
`graph_hash`. Two operators with identical state prove they fired the
byte-identical graph by comparing that one hash - the fire is a hash, not
a trigger.

Two operator verbs make the projection observable and verifiable without
running anything:

```bash
# Print the graph hash a schedule would dispatch at a given instant,
# WITHOUT firing (no journal row, no receipt, no last_fire_at mutation).
bernstein schedule show <id> --at 1700000000
bernstein schedule show <id> --at 2023-11-14T22:13:20+00:00 --json

# Replay every recorded fire and confirm each graph hash reproduces
# byte-identically from its recorded inputs. Exit 1 on any mismatch.
bernstein schedule verify
bernstein schedule verify --json   # for CI gates
```

`--at` accepts a bare integer epoch or an ISO-8601 timestamp; a naive
timestamp is read as UTC so the printed hash is host-timezone
independent.

### Recording

Each dispatched fire (in addition to the #1798 receipt + `schedule.fire`
chain entry) records, under a per-fire run id derived deterministically
from `(schedule_id, fire_time)`:

- a `schedule.fire_projection` row in the run **event journal**
  (`.sdd/runs/sched-fire-<id>/journal.jsonl`) carrying `{schedule_id,
  fire_time, last_state_hash, graph_hash}`;
- the canonical graph bytes sealed into the run **lineage spine**
  (`.sdd/lineage/sched-fire-<id>/spine.jsonl`) with a deterministic
  `timestamp = fire_time` so replay is byte-identical;
- optionally, a `schedule.fire_projection` entry in the HMAC audit chain
  binding the spine entry hash and the trigger input hash.

`schedule verify` walks the journal rows and re-runs the projection from
each row's persisted inputs, so it re-derives the fire independently of
the live schedule store.

### Recurrence rules: cron and RFC-5545 RRULE

The recurrence rule that produced the fire instant is folded into the
projection, so the `graph_hash` distinguishes a daily digest from an
hourly one. Both forms are accepted and canonicalised (part order,
`BY*` list order, and case do not perturb the hash):

- cron: `0 9 * * *` or `cron:0 9 * * *`;
- RRULE: `RRULE:FREQ=DAILY;INTERVAL=2;BYHOUR=9` (RFC-5545 subset).

Canonicalisation lives in
`src/bernstein/core/orchestration/recurrence.py` and adds no runtime
dependency (the in-tree cron parser plus a small RRULE part parser).

### Webhook and file-change triggers

A webhook or file-change trigger is the same projection with the trigger
event bound in by hash: the raw event bytes are hashed to
`trigger_input_hash` (`sha256:<hex>`) and folded into the graph identity,
so a trigger-driven fire is reproducible from `(schedule, fire_time,
state, trigger_input_hash)`. The raw event bytes are not retained; only
their bound hash is, which is what `schedule verify` replays against.

Source:

- `src/bernstein/core/orchestration/recurrence.py` - RRULE / cron
  canonicalisation.
- `src/bernstein/core/orchestration/schedule_fire_record.py` - the
  record + verify boundary (journal + lineage spine).
- `bernstein.core.security.audit_chain.record_schedule_fire_projection` -
  the additive `schedule.fire_projection` chain helper.
