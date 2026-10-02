# Tamper-evident audit trail

After an incident, or during a compliance review, someone has to show what the
orchestrator did and that the record was not edited afterwards. The record
should be checkable by a reviewer on their own machine, from the files alone,
without trusting a hosted service or the person who hands the files over.

With `BERNSTEIN_AUDIT=1`, Bernstein writes every audited event to an
append-only log in which each row is chained to the previous one with an HMAC.
`bernstein audit seal` pins a Merkle root over the log, and
`bernstein audit verify` checks both. An edited, removed or reordered row
fails verification and is named by file and line.

## How it works

| Layer | What it catches |
|---|---|
| HMAC chain (RFC 2104, SHA-256) | Each row carries `hmac` over its payload and `prev_hmac`. Changing any byte of a row, or removing or reordering rows, breaks the chain from that row onward. |
| Merkle seal | `audit seal` stores a Merkle root over the log files under `.sdd/audit/merkle/`. A later rewrite of the sealed prefix no longer reproduces that root, even if the attacker recomputed the HMACs. |
| Checkpoints | Each seal also pins a checkpoint (root and entry count). History that shrinks or is rewritten after an accepted seal is reported as tear evidence and does not clear itself. |
| External anchor and witness (optional) | An RFC 3161 timestamp or a second party's co-signature makes a rollback of the log and its checkpoints together detectable. `audit verify` says when neither is recorded. |

Where the HMAC key lives (in order): `BERNSTEIN_AUDIT_KEY_PATH`, then
`$XDG_STATE_HOME/bernstein/audit.key`, then
`~/.local/state/bernstein/audit.key`. Key handling, rotation and every flag
are in the [audit log runbook](../security/audit-log.md).

Approval decisions made through [approval cards](../integrations/approval-cards.md)
are appended to the same chain, with a hash of exactly what the approver was
shown, so the checks below cover them too.

## Worked example

Run against Bernstein 3.20.0 in a fresh `git init` plus `bernstein init`
workspace. The audited events come from a schedule that fired twice (see
[Reproducible nightly runs](reproducible-nightly-runs.md)); any audited
activity works the same way. Hashes and timestamps will differ on your
machine. Two output panels unrelated to this example (`Pool Verification`
and `Lineage Activity Status`) are cut and marked `[...]`.

### Seal, then keep working

After the first event, seal the log:

```console
$ export BERNSTEIN_AUDIT=1
$ bernstein audit seal

╭───────────────────╮
│ Merkle Audit Seal │
╰───────────────────╯
  Root hash         8098c03934f677b6ab3a0b58b042aacb65716ec526fe529bb203d0ef999e5061
  Leaves            1
  Algorithm         sha256
  Scheme            2
  Entries           1
  Sealed at         2026-09-24T18:23:07Z
  Seal file         .sdd/audit/merkle/seal-20260924T182307Z.json
  Tiles             1
  Checkpoint        pinned at 1 entries (extends previous)
```

A minute later a second event lands:

```console
$ bernstein audit show

┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Timestamp           ┃ Event         ┃ Actor               ┃ Resource                    ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-09-24T18:23:05 │ schedule.fire │ schedule_supervisor │ schedule/sched_f41d6497f0b8 │
│ 2026-09-24T18:24:04 │ schedule.fire │ schedule_supervisor │ schedule/sched_f41d6497f0b8 │
└─────────────────────┴───────────────┴─────────────────────┴─────────────────────────────┘

Showing 2 event(s) from .sdd/audit
```

### Verify the untouched log

```console
$ bernstein audit verify

╭────────────────────────────────╮
│ HMAC Chain Verification Passed │
╰────────────────────────────────╯

╭────────────────────────────╮
│ Merkle Verification Passed │
╰────────────────────────────╯
  Root hash         8098c03934f677b6ab3a0b58b042aacb65716ec526fe529bb203d0ef999e5061
  Sealed prefix     intact
  Post-seal rows    1
  Seal file         .sdd/audit/merkle/seal-20260924T182307Z.json

╭────────────────────────────────╮
│ Checkpoint Verification Passed │
╰────────────────────────────────╯
  Pinned root       8098c03934f677b6ab3a0b58b042aacb65716ec526fe529bb203d0ef999e5061
  Pinned entries    1

Audit history is not externally anchored (no RFC 3161 anchor recorded; a rollback of the chain and
the checkpoints together would leave nothing to contradict it).
To anchor: bernstein audit anchor --print-request

Audit history is not witness co-signed (no co-signature recorded; a rollback of the chain and the
checkpoints together would leave nothing to contradict it).
To witness: bernstein audit witness export --out cp.json

[...]

╭────────────────────────────────────────────╮
│ Audit Verification: PASSED                 │
│ All audit log pillars passed verification. │
╰────────────────────────────────────────────╯
```

`Post-seal rows 1` is the event written after the seal: covered by the HMAC
chain, not yet by a Merkle root. Seal again to cover it.

### Rewrite one row

Change the first (sealed) row the way someone covering their tracks would,
then verify again:

```console
$ sed -i '' '1s/Rotate the staging credentials/Rotate the production credentials/' .sdd/audit/*.jsonl
$ bernstein audit verify

╭────────────────────────────────╮
│ HMAC Chain Verification FAILED │
╰────────────────────────────────╯
  ! 2026-09-24.jsonl:1: HMAC mismatch (expected ceac49a891f1bb53…, got cb6855ef4b083533…)

╭────────────────────────────╮
│ Merkle Verification FAILED │
╰────────────────────────────╯
  ! TAMPERED: 2026-09-24.jsonl (sealed prefix hash mismatch)

╭───────────────────────────────────────╮
│ Checkpoint Divergence (tear evidence) │
╰───────────────────────────────────────╯
  ! history conflicts with checkpoint root 8098c03934f677b6… (pinned 1 entries)
  ! 2026-09-24.jsonl: first 694 bytes no longer reproduce the checkpointed leaf hash
  The audit history shrank or was rewritten since the last accepted seal.
  A fresh seal will not be produced and this conflict will not clear itself.
  After investigating: bernstein audit ack-tear --segment 2026-09-24.jsonl --offset 694 --reason
"..."

[...]

╭────────────────────────────────────╮
│ Clearance Gate Verification FAILED │
╰────────────────────────────────────╯
  ! audit chain HMAC verification failed; gate replay not attempted
  ! 2026-09-24.jsonl:1: HMAC mismatch (expected ceac49a891f1bb53…, got cb6855ef4b083533…)

[...]

╭────────────────────────────╮
│ Audit Verification: FAILED │
│                            │
│ Failing pillar(s):         │
│   ! HMAC Chain             │
│   ! Merkle Tree            │
│   ! Checkpoints            │
│   ! Clearance Gates        │
╰────────────────────────────╯
$ echo $?
1
```

Four independent checks fail and each names the file and line or byte
offset. Recomputing the row's HMAC needs the key, and even with the key the
sealed Merkle root and the pinned checkpoint still disagree unless they are
rewritten too. That last case is what an external anchor or a witness
co-signature is for.

## Put it in CI or cron

`audit verify` exits non-zero on any chain break, missing record or HMAC
mismatch:

```bash
bernstein audit verify > audit-verify.txt 2>&1 || exit 1
bernstein audit seal
```

Run the seal on a schedule so the unsealed tail stays short. For evidence
that must hold up against the log's own operator, add an
[RFC 3161 anchor or a witness co-signature](../security/audit-log.md).

## What this does and does not prove

- It proves the log has not changed since it was written (HMAC chain) and
  since it was sealed (Merkle root and checkpoint).
- Anyone holding the HMAC key and write access to `.sdd/audit/` can replace
  the log, its seals and its checkpoints with a new, internally consistent
  set. Only an RFC 3161 anchor or a witness co-signature, held outside that
  host, contradicts such a rollback.
- It records what Bernstein saw and did. It does not observe actions taken
  outside Bernstein on the same host.

## Related

- Step by step (includes sealing and verifying): [Schedule a recurring run and verify every fire](../tutorials/verified-scheduled-run.md)
- [Audit log runbook](../security/audit-log.md)
- [Verifiable audit receipts](../security/audit-receipt.md)
- [Approval cards](../integrations/approval-cards.md)
