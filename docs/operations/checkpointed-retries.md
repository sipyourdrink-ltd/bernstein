# Checkpointed retries

When a worker fails a gate or crashes, Bernstein historically restarted the
task from zero: full re-prompt, full cost, all in-context progress discarded.
Most primary CLI adapters expose native session continuation, and the adapters
already plumb session identifiers. Checkpointed retries close that gap: a
failed task's retry can resume the prior native session (**warm**), branch a
new session off the recorded checkpoint (**fork**), or restart from zero
(**cold**) -- and every one of those decisions is a deterministic,
journal-anchored record.

## Per-adapter capability

What an adapter can offer is derived from the resume strategy it already
declares in the adapter contract -- a single source of truth, so a warm retry
can never be claimed for an adapter whose `bernstein resume` is
fallback-fresh:

| Declared resume strategy | Checkpoint-retry capability |
|---|---|
| native resume + fork-capable session store | `fork` (and `warm`) |
| native resume | `resume` (warm only) |
| unsupported | `none` (always cold) |

Adapters without the capability fall back to cold with no behavior change.

## Checkpoint references

At every checkpoint the adapter's native session id and a workspace hash are
appended to the task's event journal
(`.sdd/runs/task-<task-id>/journal.jsonl`) as a Merkle-chained row. The row's
`event_hash` is the checkpoint's identity; there is no separate checkpoint
ledger. Before any row is trusted at retry time, the journal's whole chain is
re-verified -- a tampered checkpoint reference can only ever produce a cold
restart, never a warm resume of an attacker-chosen session.

## The retry decision

Each retry derives its mode as a pure function of:

1. the requested per-task policy (`warm` / `fork` / `cold`, from task
   metadata key `retry_policy`, default `warm`);
2. the adapter's declared capability;
3. the latest verified checkpoint reference;
4. a workspace-hash comparison between the checkpoint and the live worktree.

The safety valve: on a workspace-hash mismatch the retry downgrades to cold,
because provider-side session state can no longer be trusted to match the
workspace. The downgrade is recorded, never silent. Tasks pinned to
fresh-context retries (`agent_restart_between_retries`) are always cold.

Warm and fork retries send a templated corrective instruction parameterized
by the failed gate's name and output -- never freeform text -- so the resumed
session's new input is auditable and token-bounded. On the same fixture, the
warm retry prompt is a fraction of the cold re-prompt's input tokens.

## What gets recorded

Every decision (including cold downgrades) lands in three places:

* a `retry.decision` row in the task's event journal (Merkle-chained next to
  the checkpoints it consumed);
* the decision's canonical bytes sealed into the run lineage spine
  (`.sdd/lineage/task-<task-id>/spine.jsonl`) with a deterministic timestamp,
  so two identical decisions seal byte-identically;
* a `retry.checkpoint_decision` event in the HMAC-chained audit log, binding
  `{retry_mode, requested_mode, capability, checkpoint_event_hash,
  workspace_match, downgrade_reason, decision_hash}` to the journal and
  spine anchors.

Replay can therefore distinguish warm from cold retries from the chain alone,
and the retry lineage of any task is inspectable offline. Prompt content,
gate output, and provider session state are never mirrored into the chain --
only identifiers, hashes, the mode, and the downgrade reason.

## Retried-task metadata

The retry path stamps the decision onto the retried task's metadata:

| Key | When | Meaning |
|---|---|---|
| `retry_mode` | always | effective mode (`warm` / `fork` / `cold`) |
| `retry_requested_mode` | always | what the policy asked for |
| `retry_decision_hash` | always | SHA-256 of the canonical decision |
| `retry_downgrade_reason` | on downgrade | e.g. `workspace_hash_mismatch`, `adapter_capability_none`, `no_checkpoint` |
| `retry_checkpoint_session_id` | warm/fork | native session to resume |
| `retry_checkpoint_event_hash` | warm/fork | journal anchor of the checkpoint |
| `retry_corrective_instruction` | warm/fork | the templated instruction to send |

The stamp is best-effort: any failure inside the decision path degrades to a
plain `retry_mode: cold` stamp and the retry proceeds unchanged.

## Failure modes

* **Provider session TTL expired under a long gate** -- the resume attempt
  falls back to cold; correctness is unaffected because the workspace-hash
  guard and the cold path carry the full prompt.
* **Session content drifted from the recorded checkpoint** -- the
  workspace-hash comparison is the contract: mismatch means cold, recorded
  as `workspace_hash_mismatch`.
* **Journal tampered or truncated** -- chain verification fails closed; the
  retry is cold and the verification failure is logged.
