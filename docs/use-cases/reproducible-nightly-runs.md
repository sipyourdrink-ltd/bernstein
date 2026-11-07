# Reproducible nightly runs

A job fires at 02:00 every night on a build host. The next morning someone
asks three questions:

1. Did last night's run dispatch what the schedule says it should?
2. Would a second host, given the same schedule, have dispatched the same thing?
3. Has anyone edited the record of that run since it happened?

Bernstein answers all three from files on disk, offline, with exit codes you
can gate CI on.

## How it works

| Piece | What it gives you |
|---|---|
| Schedule id | Derived from the schedule body (cron, goal, scenario). The same `schedule add` on two hosts lands on the same id. |
| Fire projection | Each fire is a pure function of `(schedule_id, fire_time, last_state)` that yields a canonical task graph and its `graph_hash`. Cron evaluation runs in UTC, so host timezones do not change it. |
| `schedule show --at` | Prints the `graph_hash` a schedule *would* dispatch at a given instant without firing anything. |
| Fire records | A dispatched fire writes a receipt under `.sdd/runtime/schedule_receipts/`, a row in the run event journal, the canonical graph into the lineage spine, and (with `BERNSTEIN_AUDIT=1`) a `schedule.fire` entry in the HMAC audit chain. |
| `schedule verify` | Re-runs every recorded projection from its recorded inputs and compares the `graph_hash`. |
| `schedule audit` | Re-derives each receipt's projection, cross-checks it against its audit-chain entry, and checks receipt-to-receipt linkage. |

The full reference is [Schedules](../operations/schedule.md).

## Worked example

Everything below was run against Bernstein 3.20.0. No agent or model is
involved: registering, previewing and verifying a schedule are local
operations. Hashes and timestamps will differ on your machine.

### Two hosts agree before anything runs

Register the same nightly schedule in two independent workspaces (each a
fresh `git init` plus `bernstein init`):

```console
$ bernstein schedule add --cron "0 2 * * *" --goal "Run the nightly dependency audit and summarise findings"
Registered schedule sched_40bf84f0cd9e
  cron:     0 2 * * *
  goal:     Run the nightly dependency audit and summarise findings
  misfire:  skip
```

Both workspaces print `sched_40bf84f0cd9e`. Ask each one what it would
dispatch at tomorrow's 02:00 UTC:

```console
host-a$ bernstein schedule show sched_40bf84f0cd9e --at 2026-09-25T02:00:00+00:00
893b2d9dcd24bdaa8790782780cc7323ef55f158ac3e8c12e2202363c1f2c392

host-b$ bernstein schedule show sched_40bf84f0cd9e --at 2026-09-25T02:00:00+00:00
893b2d9dcd24bdaa8790782780cc7323ef55f158ac3e8c12e2202363c1f2c392
```

Same hash on both hosts, and nothing fired: `--at` writes no receipt, no
journal row and no audit entry. Comparing this one value is how two
operators confirm they will dispatch the same task graph. `--json` gives the
inputs alongside the hash:

```console
$ bernstein schedule show sched_40bf84f0cd9e --at 2026-09-25T02:00:00+00:00 --json
{
  "fire_time": 1790301600,
  "graph_hash": "893b2d9dcd24bdaa8790782780cc7323ef55f158ac3e8c12e2202363c1f2c392",
  "recurrence": "cron:0 2 * * *",
  "rev": "1",
  "schedule_id": "sched_40bf84f0cd9e"
}
```

### Verify a fire after it happened

To get a real fire without waiting for 02:00, the next workspace uses an
every-minute cron and a single supervisor tick, with the audit chain on:

```console
$ export BERNSTEIN_AUDIT=1
$ bernstein schedule add --cron "* * * * *" --goal "Run the nightly dependency audit and summarise findings"
Registered schedule sched_734cd9f90c4b
  cron:     * * * * *
  goal:     Run the nightly dependency audit and summarise findings
  misfire:  skip
$ bernstein schedule run --once
tick complete: 1 receipt(s), 1 fire(s)
```

Replay the recorded projection and check the receipt against the audit chain:

```console
$ bernstein schedule verify
FIRE_TIME            SCHEDULE                 GRAPH              STATUS
2026-09-24 18:15:00  sched_734cd9f90c4b       bf33d26c008de927   ok

$ bernstein schedule audit
FIRE_TIME            SCHEDULE                 PROJECTION         STATUS     CHAIN
2026-09-24 18:15:00  sched_734cd9f90c4b       4f9c8552704796b3   verified   ok
```

### An edited record does not verify

Change the goal inside the stored receipt, as someone rewriting history
would, and audit again:

```console
$ sed -i '' 's/summarise findings/push to production/' \
    .sdd/runtime/schedule_receipts/sched_734cd9f90c4b-1790273700-fire.json
$ bernstein schedule audit
FIRE_TIME            SCHEDULE                 PROJECTION         STATUS     CHAIN
2026-09-24 18:15:00  sched_734cd9f90c4b       4f9c8552704796b3   MISMATCH   ok

audit FAILED - the following receipts did not verify:
  - sched_734cd9f90c4b@1790273700: projection hash mismatch: recorded 4f9c8552704796b3… != recomputed 7840897b98bb9b98…
$ echo $?
1
```

The command names the receipt and exits non-zero. (`sed -i ''` is the macOS
form; on Linux use `sed -i`.)

## Put it in CI

Both verbs are safe as gates because they exit non-zero on any mismatch:

```bash
bernstein schedule verify --json > schedule-verify.json
bernstein schedule audit --json > schedule-audit.json
```

To check that a second host is configured identically, compare the output of
`bernstein schedule show <id> --at <instant>` from both hosts for the same
instant.

## What this does and does not prove

- It proves which task graph a fire dispatched, and that the record of it
  has not changed since.
- It does not make a model's output deterministic. Two runs of the same graph
  can still produce different agent text; the graph hash pins what was asked,
  not what was answered. Pair it with
  [signed result receipts](signed-worker-results.md) to pin what came back.
- `schedule audit` cross-checks receipts against the HMAC audit chain. Whether
  that chain itself was rewritten is answered by `bernstein audit verify`; see
  [Tamper-evident audit trail](tamper-evident-audit-trail.md).

## Related

- Step by step: [Schedule a recurring run and verify every fire](../tutorials/verified-scheduled-run.md)
- [Schedules reference](../operations/schedule.md)
- [Deterministic replay](../operations/deterministic-replay.md)
