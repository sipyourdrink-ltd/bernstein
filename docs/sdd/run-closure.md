# Authenticated run closure

## Contract

`run.closure` is Bernstein's execution-path-neutral terminal statement. It is
an HMAC-chained event for one `run_id` with exactly one outcome:
`completed`, `failed`, `cancelled`, or `abandoned`. It binds exactly one
verified state anchor:

- foreground, multi-cell, and capsule-governed runs bind the final
  `run_journal_head` and `run_journal_event_count`;
- detached RunService runs bind the final `work_ledger_head` and
  `work_ledger_entry_count`.

The marker is a statement at chain position N, not a physical write barrier.
A verifier walks the authenticated chain. Exactly one well-formed marker with
no later event for the same run derives `closed`; no marker derives `open`; a
later same-run event derives `invalidated`; duplicate, conflicting, malformed,
or tampered evidence fails closed. Interleaved events for other runs remain in
the range and do not change this run's verdict.

## Emission ownership

The component that owns the authoritative execution state owns closure:

- the foreground orchestrator records `run_completed`, seals capsule lineage,
  then appends closure as its final same-run audit event;
- the multi-cell orchestrator records its terminal journal row, then closes;
- RunService closes the durable work ledger, records the lifecycle boundary,
  and appends closure inside the same audit-chain transaction;
- the detached supervisor maps normal exhaustion, task failure, and operator
  interruption to completed, failed, and cancelled respectively.

Retrying the identical outcome and anchor returns the existing marker. A
different outcome or anchor conflicts. The read-and-append section is protected
by the audit chain's cross-process transaction, so concurrent terminal writers
cannot both observe absence and append valid competing closures.

Once a detached ledger is closed, attach, detach, daemon-restart, completion
retry, and direct frontier advancement are read-only. This prevents an
observation or retry from manufacturing a later event past the terminal
boundary.

## Abnormal termination

SIGKILL, an OOM kill, power loss, and a sleeping laptop cannot execute a
`finally` block. The foreground process therefore writes an atomic owner record
beside the journal's authenticated start. The live watchdog and later startups
may write an `abandoned` closure only after they positively observe that exact
owner PID dead and verify that the owner record matches the first row of the
named journal. A journal that already ends in `run_completed` recovers its
recorded completed, failed, or cancelled outcome instead.

Owner attribution is retained per run so a newer run cannot erase an unresolved
orphan. If process death cannot be established, or the journal or owner binding
does not verify, no closure is written and the run remains open. That is an
intentional honest gap, not a guessed outcome.

## Adversarial matrix

| Attempt | Derived result |
| --- | --- |
| No terminal marker | `open` |
| Identical concurrent/retried close | one marker, `closed` |
| Different outcome or anchor | writer refuses; retained conflict fails closed |
| Later event for the same run | `invalidated` |
| Interleaved event for another run | unchanged |
| Marker with both/no anchors, bad digest, or zero count | `invalid` |
| HMAC-chain mutation | `tampered` |
| Dead PID does not match retained owner | no marker |
| Owner start does not match the verified journal | no marker |
| Identity receipt sees only a work-ledger closure | remains `observed` |

## Trust boundary

Closure proves that the authenticated audit range contains one terminal
statement bound to a named verified state head and that no later retained event
for that run follows it. It does not prove that journaled claims describe the
outside world, observe effects that bypass Bernstein, make an HMAC key holder
honest, or turn timestamps into ordering evidence. Those are separate identity,
effect-attestation, containment, and transparency boundaries.
