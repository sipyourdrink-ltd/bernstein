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
