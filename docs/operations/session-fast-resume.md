# Session persistence (fast resume)

On a graceful stop, Bernstein snapshots which tasks were done, in-flight,
and still queued. On the next start, if that snapshot is fresh enough, the
orchestrator skips the manager's planning phase and picks up where the
previous run left off instead of re-planning the goal from scratch.

Source: `src/bernstein/core/persistence/session.py`.

## What gets saved

`SessionState`, written to `.sdd/runtime/session.json`:

| Field | Meaning |
|---|---|
| `saved_at` | Unix timestamp of the snapshot |
| `goal` | The run's goal text |
| `completed_task_ids` | Tasks that reached `done` |
| `pending_task_ids` | Tasks that were `claimed` or `in_progress` |
| `open_task_ids` | Tasks that were queued but not yet claimed |
| `cost_spent` | Cumulative USD cost for the run |

## When it is saved

- `bernstein stop` calls `save_session_on_stop()`, which queries the running
  task server for current task statuses and writes `session.json`. If the
  server is unreachable, it falls back to a lightweight diagnostic file
  (`session_state.json`) that fast-resume does not read.
- Ctrl-C during an interactive dashboard session runs the same save through
  the installed `SIGINT` handler before the process exits.
- The orchestrator also saves session state internally at points during a
  run (`orchestrator._save_session_state()`), not only at shutdown.

## When it is resumed

On the next `bernstein run` (or equivalent start path), `bootstrap.py` calls
`check_resume_session()`, which loads `session.json` unless:

- `--fresh` was passed on the command line, or
- `session.resume` is set to `false` in config, or
- the snapshot is older than the staleness threshold.

```bash
bernstein run --fresh   # ignore any saved session; start clean
```

Config (`bernstein.yaml`):

```yaml
session:
  resume: true              # default; set false to always start fresh
  stale_after_minutes: 30   # default; snapshots older than this are discarded
```

A session with unfinished work (any `pending_task_ids` or `open_task_ids`)
is never treated as a completed run on resume — the orchestrator falls
through to work the remaining backlog or re-plan the goal, rather than
declaring the run done with queued work silently dropped
(`SessionState.has_unfinished_work()`). Only a session whose queue was
fully drained short-circuits planning and reports "N done previously".

## Related, separate persistence in the same module

`core/persistence/session.py` also defines several other stop/resume
adjacent primitives that are not part of fast-resume itself:

- `WrapUpBrief` / `save_wrapup()` / `load_wrapup()` — end-of-session
  human-readable summary (`bernstein wrap-up`), written to
  `.sdd/sessions/<timestamp>-wrapup.json`.
- `save_checkpoint()` / `load_checkpoint()` — point-in-time progress
  snapshots (`bernstein checkpoint`), written to
  `.sdd/sessions/<timestamp>-checkpoint.json`. This is a different
  mechanism from checkpointed *retries* — see
  [Checkpointed retries](checkpointed-retries.md).
- `latch_session_flags()` / `load_latched_flags()` — session-stable
  feature-flag values that should not re-evaluate mid-session.
- `record_bridge_event()` / `load_bridge_lineage()` — remote-bridge
  connect/disconnect/rebuild lineage for chat/dashboard handoff (a
  different feature from [Session handoff](session-handoff.md)).
- `emit_task_notification()` / `load_task_notifications()` — structured
  terminal status reports from agents.
- `save_startup_gate_checkpoints()` / `load_startup_gate_checkpoints()` —
  per-gate enabled/disabled/cached state captured at startup, for
  diffing gate configuration between restarts.

These share the module and the `.sdd/` on-disk layout but are independent
of the `session.json` fast-resume path described above.
