# Stall escalation receipts

When a worker stalls, it leaves a signed, journal-anchored receipt that fixes
the exact failure window, so an operator can reconstruct it offline and hand it
to a postmortem.

```
bernstein escalation show   <receipt-id>
bernstein escalation verify <receipt-id>
```

## Why

In a large parallel fleet, a stalled worker today surfaces as a dashboard
signal at best, with no reconstructable record of the failure window. Bernstein
already writes one canonical, Merkle-chained event journal per run, so the
window that preceded the stall is already recorded and tamper-evident. The
escalation receipt makes that window the artefact: it binds the last N journal
entries by their Merkle hash, signs the binding with the install identity, and
anchors it in the escalation lineage spine.

## What the receipt binds

| Field | Meaning |
|---|---|
| `run_id`, `worker_id`, `session_id`, `worktree_id` | The stalled worker. |
| `stall_reason` | Structured reason from the upstream detector. |
| `recommended_action` | Deterministic action: `respawn` / `escalate` / `park` / `inspect`. |
| `from_step`, `window_entry_hashes` | The trailing window: each journal entry's `event_hash`. |
| `journal_head_at_stall` | The journal Merkle head at stall time. |
| `fork_ref` | The f03 fork point (`run_id`, `fork_step`, `snapshot_sha`) to resume from, or none. |
| `signature`, `signer_public_key_pem`, `journal_entry_hash` | Ed25519 signature and spine anchor. |

## How verify reconstructs the window

1. Check the Ed25519 signature against the receipt's embedded public key over
   the canonical binding (no operator override to the binding).
2. Re-anchor the receipt against the escalation lineage spine, and verify the
   spine chain.
3. Walk the run journal's own Merkle chain; a tampered entry surfaces as a
   chain break.
4. Recompute the trailing window from the journal at `from_step` and confirm
   the byte-identical `event_hash` list the receipt recorded.

A tampered journal entry inside the window rehashes, so the reconstructed
window diverges from the receipt and `verify` exits 2. This is the forensic
guarantee: the receipt reconstructs from the journal alone, and any edit to the
failure window is detected.

## Recommended action is deterministic

The recommended action is a pure function of the stall reason, the window
entries, and the remaining respawn budget. Two operators assembling from the
same journal prefix arrive at the byte-identical action, so the recommendation
is reproducible rather than advisory.

## Resume fork point

When the caller pins a `fork_step`, the receipt references the f03 fork point
recorded at that step (a snapshot event in the journal). Assembly refuses a
`fork_step` with no snapshot event, so a receipt never points at a fork point
that cannot resume. An operator resumes with `bernstein fork --run <run-id>
--from-step <fork_step>` (see [fork-from-step](fork-from-step.md)).

## Supervisor projection

The TUI and web supervisor surface the receipt as a projection: stall reason,
recommended action, resume fork point, and the spine anchor an operator can
hand to `bernstein escalation verify`. The projection never carries the
signature or the raw window hashes; those are recomputed by `verify`.

## Audit-chain mirror

Each emitted receipt is mirrored into the HMAC-chained audit log as an
`escalation.receipt` event, recording the run, worker, recommended action,
journal head at stall, window size, and resume snapshot sha, so an operator can
prove from the chain alone that an escalation was emitted, without recording any
journal payloads.

## Kill-decision verdicts

Before any of this machinery runs, the orchestrator must first decide to kill a
worker. That decision is mirrored into the audit chain as a `stall.verdict`
event (`core.security.audit_chain.record_stall_verdict`), written at the moment
the verdict is reached, before the kill is issued. The event carries the stall
reason, which detector fired (`heartbeat` / `stall_simple` / `stall_profiled`),
and the measured inputs that actually drove the decision (heartbeat age,
identical-snapshot count, the threshold crossed) -- each detector records only
what it measured.

A verdict attests a decision, never an outcome: the worker may still be alive
when the record lands. The stop itself is attested by the companion
`process.reap_receipt` event, and the two join on `session_id`, so an operator
reconstructing a failure window can place "this detector saw these inputs and
decided" next to "this mechanism delivered the stop" without guessing. Recording
is best-effort -- a chain failure never blocks the kill -- but never silent: the
failure surfaces as a warning naming the session.

## Automatic kills emit receipts

The three automatic stall-kill paths in `core.agents.heartbeat`
(`_escalate_heartbeat`, `_escalate_stall_simple`, `_escalate_stall_profiled`)
emit a full escalation receipt for every kill they issue, not just the verdict.
The ordering is:

1. `stall.verdict` is recorded (the decision, before the kill).
2. The worker is killed (SIGTERM/SIGKILL ladder or `spawner.kill`).
3. `escalation.receipt` is emitted (the failure window, after the kill).

The receipt assembly runs *after* the kill so the kill path is never delayed or
blocked, and inside the single-threaded tick loop nothing else appends to the
journal between the verdict and the receipt, so the receipt binds exactly the
window the verdict observed. Because the audit chain is read fresh from disk on
every append, the `escalation.receipt` event chains directly off the
`stall.verdict` that precedes it.

The automatic path reuses the same assembly and verification machinery as a
manually issued receipt -- `bernstein escalation verify <receipt-id>` works on
automatic receipts unchanged, and they appear in `bernstein escalation show`.
The receipt records the stalled worker's `session_id`, and the mirrored
`escalation.receipt` audit event carries it too, so the session link is read
from the record itself rather than reconstructed by matching ids.

The `worktree_id` on an automatic receipt is derived from the worktree the
orchestrator runs (`sha256` of the resolved workdir, truncated to 16 hex chars
-- the same convention `worktree_id_for` uses elsewhere), so a receipt stays
attributable to the worktree that processed the kill without taking a skills
catalogue dependency.

Emission is best-effort, like the verdict: a missing or empty run journal, a
failing install identity, or a chain write failure never prevents the kill
that already happened, and surfaces as a warning naming the session and
detector. Only when the orchestrator carries no `_run_id` is the skip silent
(`info`) -- without a run there is no journal to anchor, so there is nothing
to emit.
