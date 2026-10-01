# Schedule a recurring run and verify every fire

In this tutorial you register a recurring schedule, preview what it will
dispatch, let it fire once, and then prove from the files on disk that the
fire happened as recorded. Along the way you seal and verify the audit log.

No agent or model is started: the schedule supervisor records each fire and
hands it to the trigger pipeline, which a running orchestrator would pick up.
That makes this safe to run on a laptop with no API keys.

**Time:** about 10 minutes. **You need:** Bernstein installed
([Install](../getting-started/install.md)), `git`, and a shell.

Output below is from Bernstein 3.20.0. Your ids, hashes and timestamps will
differ; the shape of the output and the exit codes will not.

## 1. Make a throwaway workspace

```bash
mkdir schedule-demo && cd schedule-demo
git init -q && git commit -q --allow-empty -m init
bernstein init
```

```console
[...]
Created .sdd/config.yaml
Created bernstein.yaml
Created templates/ (default roles & prompts)
Created .gitignore (added .sdd/runtime/)
[...]
```

Turn on the audit chain and point it at a key used only for this tutorial, so
your real audit key is not touched:

```bash
export BERNSTEIN_AUDIT=1
export BERNSTEIN_AUDIT_KEY_PATH="$(mktemp -d)/audit.key"
```

!!! success "Check"
    `ls .sdd` lists `config.yaml`. Bernstein creates the key file the first
    time it writes an audited event.

## 2. Register a schedule

An every-minute cron lets you see a fire in this tutorial. For a real nightly
job you would use `0 2 * * *`.

```console
$ bernstein schedule add --cron '* * * * *' --goal 'Run the dependency audit and summarise findings'
Registered schedule sched_70a0d4a31103
  cron:     * * * * *
  goal:     Run the dependency audit and summarise findings
  misfire:  skip

$ bernstein schedule list
ID                       CRON                     POLICY     GOAL/SCENARIO
sched_70a0d4a31103       * * * * *                skip       Run the dependency audit and summarise findings
```

!!! success "Check"
    The id is derived from the schedule body. Running the same `schedule add`
    again, or on another host, gives the same id.

## 3. Preview what a fire would dispatch

`--at` computes the task-graph hash for a given instant without firing:

```console
$ bernstein schedule show sched_70a0d4a31103 --at 2026-09-25T02:00:00Z
307f9568bb46d28c0023646c9aac4ca233cbffb579a37ce8356d3d65c0665f19

$ bernstein schedule show sched_70a0d4a31103 --at 2026-09-25T02:00:00Z
307f9568bb46d28c0023646c9aac4ca233cbffb579a37ce8356d3d65c0665f19
```

!!! success "Check"
    Both calls print the same hash, and `ls .sdd/runtime/schedule_receipts/`
    does not exist yet: a preview writes nothing.

## 4. Let it fire once

`schedule run` is the long-running supervisor. `--once` runs a single tick
and exits:

```console
$ bernstein schedule run --once
tick complete: 1 receipt(s), 1 fire(s)

$ ls .sdd/runtime/schedule_receipts/
sched_70a0d4a31103-1790273940-fire.json
```

The receipt name is `<schedule id>-<fire time as epoch seconds>-fire.json`.

!!! success "Check"
    `bernstein schedule doctor` shows the last and next fire:

    ```console
    $ bernstein schedule doctor
    schedules registered: 1
    supervisor alive:     False
    last fire at:         2026-09-24 18:19:00 UTC
    next fire at:         2026-09-24 18:20:00 UTC (schedule sched_70a0d4a31103)
    ```

    `supervisor alive: False` is expected: `--once` exited after its tick.

## 5. Verify the fire

Replay the recorded projection, then cross-check the receipt against the
audit chain:

```console
$ bernstein schedule verify
FIRE_TIME            SCHEDULE                 GRAPH              STATUS
2026-09-24 18:19:00  sched_70a0d4a31103       fc1f71f90f560227   ok

$ bernstein schedule audit
FIRE_TIME            SCHEDULE                 PROJECTION         STATUS     CHAIN
2026-09-24 18:19:00  sched_70a0d4a31103       2cde21f9210c2691   verified   ok
```

!!! success "Check"
    Both commands exit `0` (`echo $?`). Any `MISMATCH` row makes them exit
    `1`, which is what lets you use them as CI gates.

## 6. Seal and verify the audit log

The fire also wrote a `schedule.fire` row to the HMAC-chained audit log:

```console
$ bernstein audit show

┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Timestamp           ┃ Event         ┃ Actor               ┃ Resource                    ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-09-24T18:19:46 │ schedule.fire │ schedule_supervisor │ schedule/sched_70a0d4a31103 │
└─────────────────────┴───────────────┴─────────────────────┴─────────────────────────────┘

Showing 1 event(s) from .sdd/audit
```

Pin a Merkle root over it, then verify everything:

```console
$ bernstein audit seal

╭───────────────────╮
│ Merkle Audit Seal │
╰───────────────────╯
  Root hash         4b629a35a1bf0571c2cdc37c27c28b58e996be915c04caca88d9ac8ea4106eeb
  Leaves            1
  Algorithm         sha256
  Scheme            2
  Entries           1
  Sealed at         2026-09-24T18:19:56Z
  Seal file         .sdd/audit/merkle/seal-20260924T181956Z.json
  Tiles             1
  Checkpoint        pinned at 1 entries (extends previous)

$ bernstein audit verify
[...]
╭────────────────────────────────────────────╮
│ Audit Verification: PASSED                 │
│ All audit log pillars passed verification. │
╰────────────────────────────────────────────╯
```

!!! success "Check"
    The last panel reads `PASSED` and the exit code is `0`. The notes about
    an external anchor and a witness are informational; see the
    [audit log runbook](../security/audit-log.md) to add either.

## 7. Watch verification fail

Copy the receipt aside, change its goal, and audit again:

```console
$ R=.sdd/runtime/schedule_receipts/sched_70a0d4a31103-1790273940-fire.json
$ cp "$R" /tmp/receipt.bak
$ sed -i '' 's/summarise findings/push to production/' "$R"
$ bernstein schedule audit
FIRE_TIME            SCHEDULE                 PROJECTION         STATUS     CHAIN
2026-09-24 18:19:00  sched_70a0d4a31103       2cde21f9210c2691   MISMATCH   ok

audit FAILED - the following receipts did not verify:
  - sched_70a0d4a31103@1790273940: projection hash mismatch: recorded 2cde21f9210c2691… != recomputed cc8c13ad35326635…
$ echo $?
1
```

The recorded hash no longer matches what the edited inputs project to, so the
fire is named and the command exits `1`. The same edit made to the audit log
row instead is caught by
`bernstein audit verify`; the
[audit trail use case](../use-cases/tamper-evident-audit-trail.md) shows that
output in full. (`sed -i ''` is the macOS form; on Linux use `sed -i`.)

Put the receipt back and confirm the audit is clean again:

```console
$ cp /tmp/receipt.bak "$R"
$ bernstein schedule audit
FIRE_TIME            SCHEDULE                 PROJECTION         STATUS     CHAIN
2026-09-24 18:19:00  sched_70a0d4a31103       2cde21f9210c2691   verified   ok
```

## What you built

- A schedule whose id and per-fire task-graph hash any host can recompute.
- A fire recorded as a receipt, a journal row, a lineage spine entry and an
  audit-chain row.
- Two gates, `schedule verify` and `schedule audit`, plus `audit verify`, all
  of which exit non-zero on a mismatch.

## Next

- Change the cron to `0 2 * * *` and run `bernstein schedule run` under your
  process supervisor, or use the `bernstein daemon` hook; see
  [Schedules](../operations/schedule.md).
- Compare `schedule show <id> --at <instant>` across two hosts:
  [Reproducible nightly runs](../use-cases/reproducible-nightly-runs.md).
