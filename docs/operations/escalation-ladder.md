# Escalation ladder (config + evidence records)

Issue #4855 — PR1 lands the ladder as **data only**. PR2 wires it into the
compaction retry patch in `agent_lifecycle` (see "Where the inputs come from").

## Why

Today a verified failure can patch at most one `fallback_model` onto a retry
task. A second failure re-runs the same tier: no ladder, no record of why an
escalation happened, and nothing that requires the hop to be justified by
evidence. This surface makes escalation an ordered, evidence-gated policy
operators can declare — without changing default behaviour for existing roles.

## Configuration

```yaml
role_model_policy:
  backend:
    model: gpt-4.1-mini
    cli: codex
    escalation_budget_usd: 25.0   # optional; unset = no ladder budget guard
    ladder:
      - model: gpt-4.1-mini
        adapter: codex            # optional; must be installed if set
        max_attempts: 1
      - model: gemini-2.5-pro
        adapter: gemini
        max_attempts: 1
      - model: o4-mini
        adapter: codex
        max_attempts: 2
```

### `fallback_model` sugar (deprecated)

```yaml
role_model_policy:
  backend:
    model: gpt-4.1-mini
    fallback_model: gpt-4.1   # sugar for a two-step ladder [model, fallback_model]
```

`ladder` and `fallback_model` are mutually exclusive. Prefer an explicit
`ladder` in new configs.

Unset `ladder` and unset `fallback_model` preserve today's behaviour
(byte-identical role policy dumps; no ladder resolution).

## Evidence must cause the advance

Moving from step N to N+1 requires a failure-evidence reference — the digest
of a verified failure artefact the run already produces. Qualifying classes:

| Class | Meaning |
|---|---|
| `verification_failure` | Red gate / test-run digest |
| `loop_verdict` | Repeated-action loop detector verdict |
| `degraded_terminal_output` | Terminal turn with empty or degraded output |

No evidence reference, or an unknown class → the hop is **refused** and the
refusal is recorded on the audit chain (`escalation.ladder_refusal`). A
retry counter with an evidence field merely attached is not enough: the
advance decision reads the evidence first.

## Chain events

| Event | When |
|---|---|
| `escalation.ladder_hop` | Evidence caused N→N+1 |
| `escalation.ladder_refusal` | Advance requested without qualifying evidence |
| `escalation.ladder_exhaustion` | Final step failed with evidence |
| `escalation.ladder_budget_stop` | Climb would exceed `escalation_budget_usd`, or spend or the next step's estimate is unknown |
| `escalation.ladder_failure_evidence` | A failure path recorded evidence for a task |

## Where the inputs come from

The compaction retry patch (`_patch_retry_with_compaction`) plans the hop from
server-side sources only:

- **Evidence** is the latest `escalation.ladder_failure_evidence` event for the
  failed task id, read from a chain that verifies. Task metadata is never
  consulted, so a `PATCH /tasks/{id}` body cannot fabricate a hop.
- **Spend** is the cost ledger's total for the task's retry lineage
  (`CostTracker.spent_for_task`), not a metadata field.
- **Next-step estimate** comes from `estimate_spawn_cost`. When a budget is set
  and either spend or the estimate is unavailable, the ladder stops with
  `escalation_budget_unpriced` instead of treating the cost as zero.

The retry patch records the decision on the chain after the PATCH lands. If
that append fails, the hop is applied without its attestation and a warning is
logged; replay then shows the model change with no matching hop event.

Hop / exhaustion payloads bind `from_step`, `to_step`, `evidence_class`,
`evidence_digest`, and `ladder_policy_version`. Replay recomputes
`hop_digest` from the canonical projection
(`bernstein.core.routing.escalation_ladder.hop_record_digest`).

## Unrunnable steps: hard failure

A ladder step that names an adapter that is not installed **fails when the
config is read**, not when escalation fires. Skipping would keep runs alive
while silently changing the policy the operator wrote. Empty `model` values
are rejected at parse time the same way.

## Adapter neutrality

Model names pass through unmodified. Nothing in the ladder assumes Claude
tier names (or any vendor's). A non-Claude adapter completing a ladder walk
must see its own model ids untouched.

## Escalation context line

On a successful advance the decision carries a one-line brief:

```text
ESCALATION: step 0->1; attempts=2; evidence=verification_failure
```

That line is part of the recorded task patch so replay reproduces the same
brief. Compaction must not step the ladder back
down: within a task attempt chain the position is monotonic.

## Not wired yet

- Changing default policy for existing roles
- The tick-loop retry path (`maybe_retry_task`); only the compaction retry
  patch consumes the ladder today
- Failure paths calling `record_escalation_ladder_failure_evidence`; until they
  do, a configured ladder refuses every hop with `missing_evidence`
