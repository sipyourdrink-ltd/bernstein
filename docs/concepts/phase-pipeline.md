# Discrete phase pipeline

A plan step can split into discrete `research → plan → implement →
verify` phases, each spawned as a fresh short-lived agent with its own
context window. Between phases, only the **distilled handoff** -
summary, decisions, constraints, open questions - passes forward. The
implement phase never sees the research transcript.

## Why it exists

When one long-running agent does research, planning, and execution in
the same window, it burns 60 k tokens reading the codebase, emits a
plan, then keeps reading on top of that bloat while implementing -
hitting compaction and degrading quality. Discrete phases keep each
context window small and let the router pick a different model per
phase: a high-reasoning model for research, a cheaper one for
implementation.

## How to use it

Add a `phases:` list to a step:

```yaml
stages:
  - name: feature
    steps:
      - role: backend
        goal: "Add streaming responses to the chat handler"
        phases: [research, plan, implement, verify]
```

The plan loader records the phases in `task.metadata["phases"]`; the
orchestrator does not yet spawn per-phase agents. A caller that drives
`PhasedRunner` runs one executor call per phase, and each phase writes a
structured artefact to `.sdd/runtime/phase_artifacts/<task_id>/<phase>.json`:

```json
{
  "summary":         "...",
  "decisions":       ["..."],
  "constraints":     ["..."],
  "open_questions":  ["..."]
}
```

The next phase's prompt is seeded with that JSON only - not the prior
phase's transcript.

Existing single-phase plans (no `phases:` field) run unchanged.

## Routing per phase

`phase_pipeline.route_for_phase(phase)` returns a `(model, effort)`
pair from the per-phase defaults (`_DEFAULT_MODEL_BY_PHASE` /
`_DEFAULT_EFFORT_BY_PHASE`):

| Phase | Default model | Effort | Why |
|---|---|---|---|
| `research` | `opus` | high | broad codebase reading, gap analysis |
| `plan` | `opus` | high | cross-cutting design |
| `implement` | `sonnet` | normal | bounded, high-throughput edits |
| `verify` | `sonnet` | normal | structured assertion runner |

Manager-supplied `task_model` / `task_effort` overrides win over the
phase default when present.

## Configuration

The `tuning.phase_pipeline.*` keys below are accepted and stored on
`defaults.PHASE_PIPELINE`, but no shipped code reads them yet: the
orchestrator does not route `phases:` steps through `PhasedRunner`, and
`PhasedRunner` takes `gate_enabled` (default `true`), `gate_max_retries`
(default `1`) and `store` (default root `.sdd/runtime/phase_artifacts`)
as constructor arguments.

| Knob | Default | Controls |
|---|--:|---|
| `tuning.phase_pipeline.enabled` | `false` | Route `phases:` steps through the phased runner. |
| `tuning.phase_pipeline.artifact_root` | `.sdd/runtime/phase_artifacts` | Distilled handoff store. |
| `tuning.phase_pipeline.gc_on_task_close` | `true` | Drop artefacts when the parent task closes. |
| `tuning.phase_pipeline.gate_enabled` | `true` | Mechanical exit-criteria gate at each phase boundary. |
| `tuning.phase_pipeline.gate_max_retries` | `1` | Re-fires of a failing phase before the task fails. |

## Limitations

- One level of phasing per task. No nested phases inside a phase.
- The handoff schema is a fixed dataclass; no LLM-based summariser
  filling in narrative prose.
- Mid-phase abort uses the existing `task_retry.py` path; there is no
  phase-specific restart granularity.
- Cross-task phase pooling (sharing a research artefact across two
  unrelated tasks) is not supported.

## Related

- Source: `src/bernstein/core/orchestration/phase_pipeline.py`
  (`route_for_phase`, `PhasedRunner`, `ArtifactStore`)
- Plan loader: `src/bernstein/core/planning/plan_loader.py`
- Model router: `src/bernstein/core/routing/router.py`
- PR #1000
