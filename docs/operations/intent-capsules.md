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
capsule. The verdict is a pure function of `(journal, capsule)`:

- every observed action class outside the capsule's allow-list, or carrying
  egress the capsule did not permit, produces one divergence at its step index;
- the `verdict_hash` is a digest over the capsule hash, the policy mode, and the
  divergence list, so two verifiers on different machines recompute the
  byte-identical verdict offline.

Deterministic replay of a run re-derives the same drift decisions at the same
step indices. No LLM call exists on the drift-decision path: a static import
guard plus a test-suite runtime profiler assertion keep it that way.

## How verify works

`bernstein intent verify <task-id>` recomputes conformance offline:

1. Load the capsule and recompute its hash; confirm it matches the
   `intent.capsule` entry recorded in the audit chain (a tampered capsule
   diverges here).
2. Verify the audit chain itself.
3. Walk the run journal's Merkle chain (a reordered or tampered journal fails
   here, with no live process required).
4. Recompute the conformance verdict from the journal and the capsule.

Exit codes: `0` conformant, `1` no capsule, `2` drift or tamper.

## Drift escalation

On divergence the monitor emits a signed escalation receipt reusing the stall
escalation shape. The receipt binds the trailing journal window by Merkle hash,
is signed with the install identity, and is anchored in the escalation lineage
spine; its `extra_binding` names the capsule hash, the verdict hash, and the
divergent events. Because it is an ordinary escalation receipt on disk it passes
`bernstein escalation verify` unchanged, so stalls and drift produce the same
class of verifiable artefact. Each emitted receipt is mirrored into the audit
chain as an `intent.drift` event.

## Rollout

The first release defaults to warn-only: drift is surfaced and escalated but not
blocked. A blocking policy is opt-in through the drift policy `mode` field.
