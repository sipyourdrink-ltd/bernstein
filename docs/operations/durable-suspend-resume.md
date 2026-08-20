# Durable suspend and resume

A long agent session sometimes has to wait on a human: a mid-flight approval, an
external review, a credential rotation, a dependency landing. Historically that
wait cost real infrastructure. The pre-spawn approval gate can halt a task only
*before* a worker exists, and the post-completion review gate only *after* it
exits; there is no human-in-the-loop halt inside a running session. So the
process, its worktree sandbox, its parallelism seat, and its budget-envelope
reservation all stayed allocated for the entire wait. On a capped pool that
reservation blocked other tasks from dispatching.

Durable suspend and resume closes that gap. A task that must wait can **park**:
the park emits an attested receipt that releases the seat, the sandbox, and the
budget envelope, and a later **resume** restores from that receipt
deterministically. The suspension itself is the artifact -- without the chain
there is no suspension, only a dead process.

## The park is a receipt, not a flag

A park is a pair of Merkle-chained journal rows plus matching HMAC audit-chain
receipts, and every infrastructure release hangs off the suspend receipt's
hash:

1. **The suspend row is the identity.** A suspend row is appended to the task's
   event journal (`.sdd/runs/task-<task-id>/journal.jsonl`) with the same row
   discipline as the checkpoint substrate: the adapter-native session id, a
   workspace hash over the worktree, the journal head, and the envelope balance
   at park time. The row's `event_hash` is the suspension's identity.
2. **The receipt binds the hash before any effect.** A `task.suspend_receipt`
   is written into the HMAC audit chain *before* a single resource is freed.
3. **Each release references the receipt.** The process is terminated and
   reaped, the sandbox is torn down, the parallelism seat is returned, and
   unspent envelope headroom is released back to the pool as a chained budget
   event -- each recorded as a `task.suspend_resource_release` row referencing
   the suspend receipt hash. **A release effect with no matching receipt is
   rejected, fail closed.**
4. **The park is persisted.** The task enters a `SUSPENDED` state recorded in
   the work ledger, so the park survives orchestrator restarts and daemon
   crashes the same way detached run state does. A parked task is deliberately
   kept out of the resume frontier: it wakes on an explicit resume, never on an
   auto-restart.

The continuity verifier separately reports `journal_ok` (the parsed chain is
consistent and the tolerant reader discarded no input) and
`journal_identity`. Suspend/resume audit receipts independently bind the exact
rows needed for the continuity proof, but the task journal has no terminal-head
seal, so its whole-journal identity remains `unverifiable`. That is a coverage
verdict, not a failed park.

## Released headroom

The headroom returned to the pool is the reservation minus the spend recorded
against it at park time (clamped at zero):

```
released_usd = max(reserved_usd - spent_usd, 0.0)
```

It appears as a chained budget event, and because no cost is recorded while the
task is parked, the per-envelope rollup attributes **zero spend to the parked
window**. On a capped pool the freed headroom is immediately dispatchable to a
queued task.

## Resume is a deterministic projection

`bernstein task resume` re-materializes the continuation and derives its mode as
a pure function of the suspend row and the adapter capability -- the same
warm/fork/cold decision the [checkpointed retry](checkpointed-retries.md) path
uses:

| Condition | Effective mode |
|---|---|
| Same workspace hash + live native session | `warm` |
| Fork requested + fork-capable adapter | `fork` |
| Stale session, drifted workspace, or no capability | `cold` (with a recorded reason) |

The decision carries a stable `decision_hash`: two hosts with the same suspend
row and adapter capability derive the byte-identical decision. Downgrades are
never silent -- a fork or cold resume always records its reason.

The resume writes a `task.resume_receipt` binding the suspend receipt it
continued from, the effective mode, and the new workspace hash. The suspend and
resume receipt pair is the continuity proof.

## Wake on approval

`bernstein task suspend <task-id> --until approval` composes the park with the
pre-spawn approval sentinel. The parked task refuses to resume until
`bernstein approve <task-id>` lands its decision file; the resume receipt binds
the approval decision digest, and a `<task-id>.resumed` marker binds the resume
receipt hash, so the approval record and the resume receipt reference each
other. This makes approval checkpoints usable mid-session, not only at the
pre-spawn and post-completion boundaries.

## Verifying continuity offline

`bernstein audit verify-suspension <task-id>` proves, from a copied chain and
the task journal alone, that a resumed task continued from exactly the parked
workspace hash -- or shows the recorded fork/cold downgrade with its reason. No
worker, no network, and no live worktree are required. Two tamper posture
guarantees back the proof:

- Mutating the suspend **row** breaks the journal Merkle chain at that exact
  index, and the fail-closed read refuses to fuel a resume from a tampered row.
- Mutating the suspend **receipt** breaks the HMAC audit chain at that exact
  position, which `bernstein audit verify` reports.

The outcome is a tri-state on the `status` field, so a caller can branch on it
without parsing messages:

| `status` | meaning | exit code |
|---|---|---|
| `verified` | a settlement happened and its proof holds | 0 |
| `pending` | the park has not settled yet, nothing to prove | 0 |
| `failed` | a settlement is claimed but its evidence does not hold | 1 |

`failed` is reserved for a real break: a resume receipt hanging off another
park's suspend receipt, a receipt naming a suspend or resume row the task
journal does not hold, a park carrying more than one settlement, or a broken
chain or journal.

A live park reports `pending`, not `failed`. It is an incomplete lifecycle
rather than a broken proof, and reporting it as a failure would bury real
breaks when sweeping a fleet that has parked tasks in it. The `ok` field means
"no integrity failure found" and so covers both `verified` and `pending`; test
`status == "verified"` when you need a settled, proven continuity.

The distinction between `pending` and `failed` is which suspend row a resume
receipt *claims*, not merely whether any resume exists: a task parked twice
with only the first park settled leaves the second park `pending`.

## What the resume path refuses

The suspend receipt is selected by identity, never by recency, and is checked
before anything is written:

- A receipt bound to a **different suspend row** or a **different task** is
  refused, so a task parked more than once cannot resume against the wrong
  park and a substituted receipt has nothing to match.
- A receipt hash **absent from the audit chain** is refused; a non-empty hash
  is not evidence on its own.
- A park recorded `--until approval` refuses to append a resume row until the
  approval decision has landed. The gate is enforced where the mutation
  happens, not only at the call site.
- A park settles **once**. If a `task.resume_receipt` already hangs off the
  suspend receipt, a second resume is refused. The decision file records that
  the operator approved, not how many times that approval may be spent, so the
  settlement record on the chain is what bounds it to one. To resume again,
  park again and obtain a fresh suspend receipt.
- A `task_id` that is not a plain identifier is refused outright rather than
  sanitised, so it can never be used to read or write an approval record
  outside `.sdd/runtime/approvals`.

Every one of these refusals lands before the journal is touched, so a refused
resume leaves the task's Merkle chain byte-identical to the parked state.

## The grant the park binds

A park also writes an agent checkpoint under
`.sdd/runtime/agents/<task-run-id>/checkpoint.json`, carrying the role the task
ran under and a `grant_hash` over that role's permission set, the task id, the
owning run, and the suspend row's hash. The resume recomputes that hash from
the role and refuses when it no longer matches -- so a permission set narrowed
while the task was parked stops the resume before its first side effect.

The role is read from the task log (`.sdd/runtime/tasks.jsonl`), and the owning
run from `$BERNSTEIN_RUN_ID`. `--role` and `--parent-run-id` pin either one
explicitly.

When neither the log nor the flag names a role, the checkpoint is written with
an empty `grant_hash` and the resume treats it as not grant-bound. That is
deliberate: the empty role resolves to the *unrestricted* permission set, which
the resume would re-derive identically, so hashing it would produce a
checkpoint that looks bound and can never refuse. Absence stays absence.

A resume that clears that check appends one more journal row,
`task.grant_continuation`, binding the checkpoint's own hash, its `grant_hash`,
and both chain heads -- the suspend row's and the resume row's. It is what lets
a verifier holding only a copy of the journal chain the two halves of the run
together, without reading `.sdd/runtime/`. A park that wrote no grant produces
no continuation row, and a task with no checkpoint at all produces none either;
in both cases the verifier reads the resumed run as a new run rather than as an
attested continuation.
## Commands

```bash
# Park a running task, releasing its seat, sandbox, and budget envelope.
bernstein task suspend T-abc123 --reserved-usd 10 --spent-usd 2.5

# Park until an operator approves the wake.
bernstein task suspend T-abc123 --until approval
bernstein approve T-abc123          # lands the wake decision

# List durably-parked tasks with their parked-at hash and freed resources.
bernstein task list-suspended

# Resume from the attested suspend receipt (warm when the workspace matches).
bernstein task resume T-abc123

# Prove the continuation is the same run, offline.
bernstein audit verify-suspension T-abc123
```

## Repairing a crash-torn journal

A crash partway through an append can leave the run journal with a truncated
final line -- a JSON fragment with no trailing newline. The tolerant reader
discards that physical line, and `EventJournal.resume` refuses the journal (its
chain coverage is no longer complete), so the task would be unresumable for
good. The repair path truncates exactly that fragment and nothing else:

```bash
bernstein replay repair <RUN_ID>
```

The repair is safe because the fragment is not covered by any `event_hash`:
removing it restores exactly the bytes the surviving chain head already commits
to, so the head before the crash equals the head after, byte for byte. It is
also deliberately conservative:

- A discard **anywhere but the end of the file** is corruption, not a torn
  write, and the repair refuses it -- a hole in the middle of the journal must
  never be silently truncated into a shorter "valid" journal.
- If an external seal exists (the journal was finished and sealed) and the
  truncated result would not match it, the repair is refused **before writing**
  anything, so the evidence survives for inspection.
- A journal with nothing to truncate reports a no-op.

The repair is explicit-only by design: an orchestrator that silently truncates
journals to keep going would be a worse failure than a stuck task, so nothing
in the suspend/resume path runs this automatically. `bernstein replay repair`
is the one place a torn journal can be made resumable again.


## What this buys you

Runs that wait on humans stop paying for the wait: an overnight approval halt
holds zero seats, zero sandboxes, and zero reserved envelope headroom, so capped
pools keep dispatching. And every park is explainable afterwards -- the suspend
and resume receipt pair proves the continuation is the same run, so audits of
long-lived runs no longer contain unexplained process gaps.

## Relationship to the steering pause

The in-place steering pause is the *momentary* halt for quick correction: it
checkpoints a worker and parks its claim but keeps the infrastructure reserved.
Durable suspend is the variant that frees infrastructure and proves continuity.
Both share the checkpoint row shape and the receipt-before-effect rule, so a
steering pause that exceeds a wait threshold can upgrade into a durable suspend.
