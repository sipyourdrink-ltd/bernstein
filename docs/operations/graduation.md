# Pilot-to-production graduation

Bernstein sessions can operate at four increasingly trusted stages —
`sandbox`, `shadow`, `assisted`, `autonomous` — and a session graduates from
one to the next only after it clears a metrics threshold (task count, success
rate, consecutive-failure ceiling, minimum time-at-stage). Graduation is
exposed as a REST surface; there is no `bernstein graduation` CLI command.

## Stage semantics

| Stage | What runs |
|---|---|
| `sandbox` | Agents spawn but make no real changes (`dry_run=True`). |
| `shadow` | Agents run and produce diffs; changes apply locally but are not committed. |
| `assisted` | Changes are committed; each task merge requires explicit human approval. |
| `autonomous` | Changes are committed and auto-merged after batch review. |

`stage_to_orchestrator_overrides(stage)` maps each stage to the
`OrchestratorConfig` fields that enforce it (`dry_run`, `approval`,
`merge_strategy`), so a stage is not just a label — it changes what the
orchestrator is allowed to do.

## Endpoints

| Endpoint | Does |
|---|---|
| `GET /graduation/status` | All tracked sessions, current stage, and whether each can graduate. |
| `GET /graduation/config/policies` | The active per-stage thresholds. |
| `GET /graduation/{session_id}` | Stage, accumulated metrics, promotion log, and graduation readiness for one session. |
| `POST /graduation/{session_id}/record-event` | Record a task success/failure against the session's current-stage metrics. Body required: `task_id` (string) and `success` (bool) are mandatory; `duration_s`, `cost_usd`, and `initial_stage` default to `0.0`, `0.0`, and `"sandbox"`. |
| `POST /graduation/{session_id}/promote` | Manually promote a session to the next stage. Returns 409 if already `autonomous`. Send a body (`{}` is enough); `reason` and `promoted_by` default to `"manual"` and `"operator"`. |

Both `POST` endpoints validate their body, so calling them with no body — or
with `success` but no `task_id` — returns `422`, not `400`. A session record
is created on the first `record-event`; `GET /graduation/{session_id}` before
that returns `404`.

```bash
# record three successful tasks, then promote out of sandbox
curl -X POST "$BASE/graduation/$SESSION/record-event" \
  -H "Content-Type: application/json" -d '{"task_id":"t1","success":true}'
curl -X POST "$BASE/graduation/$SESSION/promote" \
  -H "Content-Type: application/json" -d '{}'
```

## Default policies

| Stage | Min tasks | Min success rate | Max consecutive failures |
|---|---|---|---|
| `sandbox` | 3 | 80% | 3 |
| `shadow` | 5 | 85% | 2 |
| `assisted` | 10 | 90% | 2 |

`autonomous` is terminal — it has no outbound policy, and `promote` on an
already-`autonomous` session returns `409`.

`can_graduate` also enforces `min_hours` (minimum wall-clock time at the
current stage) where configured; the default policies above set it to `0`
(no time requirement).

## How promotion happens

A caller records each task outcome via `record-event`
(`success`, `duration_s`, `cost_usd`). The graduation store accumulates
per-stage metrics (`tasks_completed`, `tasks_failed`,
`consecutive_failures`, `success_rate`, `hours_elapsed`) and evaluates them
against the stage's policy on every read of `GET /graduation/{session_id}`
or `/graduation/status`. Promotion itself is never automatic — a call to
`POST /graduation/{session_id}/promote` is required even once
`can_graduate` reports `true`. Every promotion appends an entry (from-stage,
to-stage, timestamp, reason, promoted-by, metrics snapshot) to the session's
promotion log.

## Persistence

- `.sdd/graduation/<session_id>.json` — current stage and per-stage metrics for one session.
- `.sdd/metrics/graduation.jsonl` — append-only event log (`task_event` and `promotion` entries).

## Read-only sealed-journal observations

`derive_graduation_observations` in `core.quality.graduation` accepts a
`JournalLoadResult`, a `GraduationEvidenceWindow` and a declared stage. The
window names one journal, an **independently obtained, trusted `JournalSeal`**,
and inclusive first/last event indices. The caller chooses the window and
stage; neither stage attribution nor seal provenance is inferred. The
`derive_graduation_observations_from_path` wrapper reads the journal once and
passes that same snapshot to verification and projection.

Before selecting the window, the projection verifies the complete journal
chain and matches its head and event count to the supplied seal. Tampering,
discarded rows, complete-row truncation, missing/mismatched seals and invalid
ranges refuse with the requested window in the error. A surviving valid
prefix cannot hide a damaged tail. Build the seal from an independent finished
journal commitment, never from the candidate rows being verified.

The result reports **observed event rows**, rather than distinct tasks:
`task_completed`, `task_failed`, and the trailing streak of explicit
`task_failed` rows since the last `task_completed`. Each count cites absolute
event indices; zero counts carry an empty range. Nonterminal and unknown
events remain listed as `unsupported_event_types`. In particular,
`task_verification_failed` is an attempt rejection and is never relabeled as
a terminal failure. Existing orchestration records verification rejections;
this projection does not add terminal-failure producers or claim complete
outcome coverage.

`canonical_bytes()` includes the rule version, declared stage and sealed
window, and is independent of the current directory and clock.
`compare_stored(StageMetrics)` reports numeric matches/differences against
the mutable completion, failure and streak counters. Those comparisons are
diagnostic: every result states `stage_metrics_parity: not_established` and
lists unsupported metrics. Equal numbers do not establish stage attribution
or complete outcome coverage. Journal envelope timestamps are unauthenticated;
elapsed hours are therefore unsupported. Costs and durations are unsupported
too; summing the existing completion `cost_usd` field would sum cumulative
run spend. This partial implementation of #5475 does not produce promotion
receipts or change the evaluator, HTTP routes or stage-change authority.

## Limitations

- No CLI wraps these endpoints; an operator (or an external dashboard)
  drives graduation via the REST API directly.
- Nothing in the orchestrator calls `record-event` automatically — a caller
  (dashboard, CI job, or operator script) is responsible for reporting task
  outcomes into the graduation store. Without that wiring, a session's
  graduation state never advances on its own.

## Source

- `src/bernstein/core/quality/graduation.py` — stages, policies, `GraduationEvaluator`, `GraduationStore`.
- `src/bernstein/core/routes/graduation.py` — the FastAPI routes listed above.
