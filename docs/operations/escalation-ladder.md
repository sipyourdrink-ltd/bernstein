# Escalation ladder (config + evidence records)

Issue #4855 — PR1 lands the ladder as **data only**. Retry wiring in
`agent_lifecycle` is unchanged; a follow-up PR consumes these types.

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
| `escalation.ladder_budget_stop` | Climb would exceed `escalation_budget_usd` |

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

When retry wiring lands, that line is part of the recorded task patch so
replay reproduces the same brief. Compaction must not step the ladder back
down: within a task attempt chain the position is monotonic.

## Out of scope (this PR)

- Changing default policy for existing roles
- Wiring `agent_lifecycle` retry patching to consume the ladder (follow-up)
