# Intent capsules

Approving an unattended run approves a sentence, not a contract. An intent
capsule compiles the approved goal into a canonical, signed chain entry and
binds the running worker's action stream to it, so scope drift is caught at the
first divergence rather than during a post-hoc journal review.

```
bernstein intent show   <task-id>
bernstein intent verify <task-id>
```

## Why

Plan approval signs off cost and risk, attested approvals cover single tool
calls, and hook gates cover completion, but nothing bound the running worker's
action stream to the goal the operator actually approved. Post-hoc journal
review does not scale to fleets, and "the run finished green" says nothing about
whether the actions taken were the actions authorized. Operators running
compliance-sensitive workflows need to demonstrate that what ran matches what
was approved, at the granularity of the run.

## What the capsule binds

The capsule is compiled at approval time from the approved `TaskPlan` and carries
the same canonical byte form as a lineage entry.

| Field | Meaning |
|---|---|
| `task_id`, `plan_id` | The task and the approved plan the capsule compiled from. |
| `goal_digest` | `sha256:` digest of the approved goal text (never the text). |
| `allowed_action_classes` | Capability surfaces the worker may use (`fs.read`, `git.commit`, ...). |
| `file_scope_globs` | Globs the worker's writes are scoped to. |
| `permitted_adapters` | Adapter names permitted to execute. |
| `egress_classes` | Egress axes permitted (empty means no outbound communication). |
| `cost_envelope_ref` | `sha256:` reference to the approved cost envelope. |
| `expiry_ts` | Timestamp after which the capsule is stale. |

The capsule is written to the HMAC audit chain as an `intent.capsule` event, and
its hash is bound into the run journal (`intent.capsule_bound`), so every
subsequent journal step is attributable to one approved capsule.

## The conformance verdict is a pure projection

The drift monitor maps each observed journal event to an action class through a
static, reviewed table (no inference, no model) and compares it against the
capsule. A recognised tool name resolves through the reviewed table first; a
worker-stamped `action_class` is honoured only for a tool the table does not
know, so a worker cannot relabel its own actions to fit the capsule.

The verdict is a pure function of `(journal, capsule, policy)`. Every constraint
the capsule declares is enforced, and each violation produces one divergence at
its step index:

| Divergence reason | Raised when |
|---|---|
| `action_class_not_permitted` | The action class is outside `allowed_action_classes`. |
| `egress_not_permitted` | An outbound-communication class ran without `external_comm` in `egress_classes`. |
| `adapter_not_permitted` | The event's adapter is outside `permitted_adapters`. |
| `adapter_unrecorded` | The event named no adapter while an allowlist is declared. |
| `file_scope_violation` | A mutating file action touched a path outside `file_scope_globs`. |
| `path_unrecorded` | A mutating file action named no path while a scope is declared. |
| `unclassified_event` | The event maps to no action class and the policy sets `allow_unclassified: false`. |

Scope globs are matched segment-wise: `*` and `?` stop at `/` and `**` spans
directories, so `src/*.py` does not silently admit `src/nested/deep.py`. Paths
are lexically normalised before matching, so an in-scope prefix cannot be used as
a free pass (`src/pricing/../../etc/passwd` does not match `src/pricing/**`), and
a path that still escapes upward after normalisation is never in scope. The check
fails closed: when a scope is declared, a mutating action that records no
recognised path is a `path_unrecorded` divergence rather than a silent pass. An
empty `file_scope_globs` declares no file scope and constrains nothing. Reads are
not scope-checked: the capsule scopes the worker's mutations.

Both the file-scope and adapter checks fail closed on absent input: with a
constraint declared, an action that records no path or no adapter is a
divergence rather than a silent pass. An allowlist that applies only when the
worker volunteers the field being checked is not an allowlist, and the journal is
written by the same worker whose conformance is being judged.

Absolute paths never satisfy a workspace-relative glob. Rewriting `/tmp/evil`
into `tmp/evil` would reinterpret the input into the shape that passes, so the
check rejects it instead.

Tool names are normalised (whitespace stripped, case folded) before the reviewed
map is consulted, so `"Bash "` cannot dodge the map and pick up a worker-stamped
label instead. The stamped label remains the fallback for tool names the map does
not know at all, so a worker that invents an unmapped name can still self-declare
a class the capsule allows; `allow_unclassified: false` surfaces those calls.
Extend the reviewed map as tools are adopted.

Only events that claim to be actions (they carry a `tool` or `action_class`, or
their type is in the action-bearing vocabulary) can become `unclassified_event`.
Structural journal rows -- ticks, snapshots, retry decisions, worktree reaps, the
capsule-bound anchor -- are never drift, whatever the policy, so
`allow_unclassified: false` stays usable on a real run.

The `verdict_hash` is a digest over the capsule hash, the policy mode, and the
divergence list, so two verifiers on different machines recompute the
byte-identical verdict offline. Timestamps are read off the journal rows rather
than a clock, which keeps expiry enforcement deterministic.

Expiry is deliberately **not** part of the verdict. A journal row's `ts` sits
outside the hashed payload (a faithful replay differs only in timing), so feeding
it into `verdict_hash` would sign a value anyone can edit in place without
breaking the chain, and would make the same run verdict differently on replay.
Expiry is instead enforced against audit-chain entry timestamps, which are
covered by each entry's HMAC. The trade-off is coverage: that detects a capsule
still being acted on past expiry only as far as the chain records events for it,
not per journal step. Per-step expiry would need a chained step clock inside the
hashed payload.

Deterministic replay of a run re-derives the same drift decisions at the same
step indices. No LLM call exists on the drift-decision path: a static import
guard plus a test-suite runtime profiler assertion keep it that way.

## How verify works

`bernstein intent verify <task-id>` recomputes conformance offline:

1. Load the capsule and recompute its hash; confirm it matches the
   `intent.capsule` entry recorded in the audit chain (a tampered capsule
   diverges here).
2. Verify the audit chain itself.
3. Resolve which run the capsule governs from the **signed** `intent.capsule`
   entry. The on-disk capsule record carries a `run_id` too, but it is unsigned,
   so it is only cross-checked: a record naming a different run than the chain
   fails as tampering rather than redirecting the verifier.
4. Walk that run journal's Merkle chain (a reordered or tampered journal fails
   here, with no live process required).
5. Require exactly one `intent.capsule_bound` anchor in the journal matching this
   `task_id` and capsule hash, so an unrelated clean journal cannot be presented
   as this capsule's run.
6. Require exactly one `intent.journal_seal` entry for this run and capsule, and
   check the journal's head hash and event count against it.
7. Recompute the conformance verdict from the journal and the capsule.

Exit codes: `0` conformant, `1` no capsule, `2` drift or tamper, `3` cannot
verify (no audit key).

## Sealing a run

A run must be sealed before it can be verified:

```python
seal_run_journal(chain=chain, sdd_dir=sdd, task_id=task_id, run_id=run_id, capsule=capsule)
```

This records the journal's head hash and event count into the audit chain. It is
not bookkeeping. The journal's Merkle chain recomputes from genesis using
positional indices, so **every prefix of a valid journal is itself a valid
journal**: a worker that drifted can delete the trailing rows that convict it and
present a shorter, internally consistent history that verifies cleanly. A proof
that reads only what remains cannot detect what was removed, so the verifier
needs an independent commitment to the journal's end.

An unsealed run therefore does not verify -- absent commitment is a denial, not a
pass. Sealing refuses a journal whose own chain does not verify, and both
truncation and post-seal appends are rejected by the count and head comparison.

The seal is also what gives capsule expiry teeth. An approval entry is by
construction at or before the expiry it declares, so scanning approvals alone
could only ever flag a capsule minted already-expired. The seal timestamp, which
is covered by its entry's HMAC, evidences a capsule still being acted on after it
expired.

`verify` is read-only. It never writes to the chain and never creates audit key
material: a freshly minted key cannot authenticate an existing chain, so
generating one would report a missing-key setup error as tampering. On a machine
without the key, point `BERNSTEIN_AUDIT_KEY_PATH` at the key the chain was
written with.

## Drift escalation

On divergence the monitor emits a signed escalation receipt reusing the stall
escalation shape. Both the capsule and the verdict handed to it are treated as
claims. Recomputing the verdict alone would not be enough, because a verdict only
means anything relative to a capsule: a caller who supplies a fabricated capsule
permitting nothing can hand in a self-consistent verdict and have a conformant
run attested as drift. The capsule is therefore resolved through the same
authority `verify` uses -- it must match the `intent.capsule` chain entry, the
run must be the signed one, and the journal must carry the matching capsule-bound
anchor. Only then is the verdict recomputed and that recomputed verdict signed.

The receipt binds the trailing journal window by Merkle hash,
is signed with the install identity, and is anchored in the escalation lineage
spine; its `extra_binding` names the capsule hash, the verdict hash, and the
divergent events. Because it is an ordinary escalation receipt on disk it passes
`bernstein escalation verify` unchanged, so stalls and drift produce the same
class of verifiable artefact. Each emitted receipt is mirrored into the audit
chain as an `intent.drift` event.

## Rollout

The first release defaults to warn-only: drift is surfaced and escalated but not
blocked. A blocking policy is opt-in through the drift policy `mode` field.
