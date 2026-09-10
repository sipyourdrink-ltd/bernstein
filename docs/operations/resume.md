# Resume (task checkpoint resume)

Audience: operators who killed an in-flight task (or had one crash) and
want to pick up from the last successful step instead of restarting.

## Overview

`bernstein resume <task-id>` loads the per-task checkpoint written by
the orchestrator after every successful step transition, validates it,
bumps `resume_count`, fires the `task.resume` lifecycle event, and
hands control back so the orchestrator can re-spawn the task from the
next step boundary.

v1 scope is **local-only**. Out of scope: cross-machine resume,
distributed checkpoint storage, resuming across role-definition
changes.

Source: `src/bernstein/cli/commands/resume_cmd.py`,
`src/bernstein/core/persistence/task_resume.py`.

## CLI

```text
bernstein resume TASK_ID
  [--workdir DIR]              project root; defaults to cwd
  [--json]                     emit machine-readable JSON instead of the Rich summary
  [--dry-run]                  validate, bump resume_count, print plan; do not re-spawn
  [--override-interpreter]     resume even though adapter/model moved since suspend
  [--override-observations]    resume even though source bytes moved since suspend
  [--discard]                  drop checkpoint first to run fresh instead of continuing
```

Re-spawn is dispatched by writing a signal file under
`.sdd/runtime/resume/`. The worker watching that directory atomically
claims it. If no worker is running, the signal persists until
`bernstein run` starts and picks it up.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Resume prepared (and, unless `--dry-run`, dispatched) |
| 2 | No checkpoint on disk for `task_id` |
| 3 | Checkpoint corrupt or failed schema validation |
| 4 | `task.resume` lifecycle hook failed |
| 5 | Grant mismatch — role narrowed, task reassigned, or parent cancelled |
| 6 | Observations moved — discard and respawn, or `--override-observations` |

## Checkpoint layout

`.sdd/runtime/checkpoints/<task-id>/checkpoint.json`. Written
atomically (`tempfile` -> `os.replace` -> `fsync`) after every
successful step. Pydantic schema with `extra="forbid"` so any
reader / writer drift fails fast.

| Field | Use |
|-------|-----|
| `schema_version` | Forward-compat marker (v1) |
| `task_id` | Task this checkpoint belongs to |
| `last_completed_step_id` | Last step the orchestrator confirmed done; resume picks up at the *next* boundary |
| `trace_cursor` | Byte offset into `.sdd/traces/<task-id>.jsonl` |
| `scratchpad_path` / `scratchpad_sha256` | Pointer + content hash for recovered scratchpad |
| `adapter` / `adapter_session_id` | Adapter + session id captured at first spawn |
| `worktree_path` | Absolute path to preserved worktree |
| `resume_count` | Bumped by `bernstein resume` before re-spawn; dashboard flags flaky tasks |
| `merge_cursor` | Reserved for `core/streaming_merge.py` coordination |
| `meta` | Adapter-opaque k/v bag |
| `created_at` / `updated_at` | ISO-8601 UTC |

## Adapter capability matrix

Adapters that implement the `resume()` capability slot pick up the
existing session. Adapters without `resume()` fall back to a fresh
spawn with the recovered scratchpad re-injected as context. The
capability matrix is at `src/bernstein/adapters/_contract.py` -
`resume_capability`.

## Examples

Dry-run a resume plan to inspect the recovery state:

```bash
bernstein resume task-9f3a --dry-run
```

Resume from a specific workdir (e.g. a fleet supervisor host):

```bash
bernstein resume task-9f3a --workdir /srv/bernstein/project-alpha
```

Resume and capture the plan for an automation:

```bash
bernstein resume task-9f3a --json > /tmp/resume-9f3a.json
```

## Troubleshooting

**`No checkpoint:` (exit 2).** The task either never produced a
checkpoint (no completed steps before the kill) or the checkpoint dir
was wiped. The only recovery is a fresh run.

**`Corrupt checkpoint` (exit 3).** Schema validation failed; remove
`.sdd/runtime/checkpoints/<task-id>/checkpoint.json` and run the task
fresh, or fix the file by hand if you know what drifted.

**Resume "dispatched" but nothing happens.** No worker is consuming
`.sdd/runtime/resume/`. Start `bernstein run` (or the relevant worker)
and the signal will be claimed.
