# Resume (task checkpoint resume)

Audience: operators who killed an in-flight task (or had one crash) and
want to pick up from the last successful step instead of restarting.

## Overview

`bernstein resume <task-id>` loads and validates the task checkpoint,
increments `resume_count`, and writes a local resume signal.

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
  [--discard]                  discard the agent checkpoint before preparing the resume
```

Each signal is published with an atomic rename under `.sdd/runtime/resume/`.
Readers see a complete request even if a later write is interrupted. A queued signal
confirms the file write. Runtime acknowledgement is currently unavailable
for these signals.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Resume prepared (and, unless `--dry-run`, signal queued) |
| 1 | I/O error while preparing the resume or writing the signal |
| 2 | No checkpoint on disk for `task_id` |
| 3 | Invalid checkpoint data, or multiple agent checkpoints match an explicit `--discard` |
| 4 | `task.resume` lifecycle hook failed |
| 5 | Grant mismatch — role narrowed, task reassigned, or parent cancelled |
| 6 | Observations moved since the agent checkpoint was written |

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

A successful dry run persists an incremented `resume_count` and prints
the plan. It returns before writing the resume signal.

Resume from a specific workdir (e.g. a fleet supervisor host):

```bash
bernstein resume task-9f3a --workdir /srv/bernstein/project-alpha
```

Capture the local resume result for automation:

```bash
bernstein resume task-9f3a --json > /tmp/resume-9f3a.json
```

Stdout contains one JSON object. `dispatch_status` is `queued` after
writing a signal and `not_dispatched` if no signal was written.
`signal_paths` lists the files written by this invocation.
`runtime_acknowledged` is currently `false`. A refusal or write failure
includes an `error` field and a nonzero exit status. With `--discard`,
`checkpoint_discarded` reports whether the agent checkpoint was removed.
A deletion or lookup I/O error stops the command before it increments
`resume_count` or queues a signal. A missing agent checkpoint allows
the task resume to proceed. If multiple agents have checkpoints for the
same task, `--discard` exits with code 3 and preserves them all.

## Troubleshooting

**`No checkpoint:` (exit 2).** The task either never produced a
checkpoint (no completed steps before the kill) or the checkpoint dir
was wiped. The only recovery is a fresh run.

**`Corrupt checkpoint` (exit 3).** Schema validation failed; remove
`.sdd/runtime/checkpoints/<task-id>/checkpoint.json` and run the task
fresh, or fix the file by hand if you know what drifted.

**Observations moved (exit 6).** Use `bernstein run --fresh` to start a
new session. To continue from the saved task step and adapter session,
`bernstein resume <task-id> --discard` removes the agent checkpoint.
`--override-observations` keeps it and accepts the changed bytes.

**Resume signal queued.** Check `signal_paths` for the file written by
the command. The runtime currently provides no receipt for that signal.
