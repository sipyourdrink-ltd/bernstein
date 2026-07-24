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
| `POST /graduation/{session_id}/record-event` | Record a task success/failure against the session's current-stage metrics. |
| `POST /graduation/{session_id}/promote` | Manually promote a session to the next stage. Returns 409 if already `autonomous`. |

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
