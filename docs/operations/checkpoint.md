# Session checkpoint

`bernstein checkpoint` writes a human-readable, point-in-time snapshot of
session progress — what's done, what's in flight, what's next, and the
current git SHA — to `.sdd/sessions/<timestamp>-checkpoint.json`.

## Usage

```bash
bernstein checkpoint                 # snapshot now
bernstein checkpoint --goal "ship the auth refactor"
```

| Flag | Default | Meaning |
|---|---|---|
| `--goal TEXT` | none | Goal label embedded in the checkpoint. |

The command requires a reachable Bernstein task server (it queries
`/tasks` for task status); it exits with an error if the server cannot be
reached or rejects the request's credentials.

## What gets captured

The command queries the task server for tasks in `done`, `claimed`, and
`in_progress`, and `open` status, resolves the current `git rev-parse HEAD`,
and writes a `PartialState` record (internally aliased as `CheckpointState`
for backward compatibility) with:

| Field | Source |
|---|---|
| `timestamp` | Wall-clock time of the snapshot |
| `goal` | `--goal`, or empty |
| `completed_task_ids` | Tasks with status `done` |
| `in_flight_task_ids` | Tasks with status `claimed` or `in_progress` |
| `next_steps` | Titles of tasks with status `open` |
| `git_sha` | Current `HEAD` commit |

After writing, it prints a Rich summary panel to the terminal listing done,
in-flight, and next-step tasks (next steps capped at 5 in the terminal view;
the full list is in the JSON file).

## What this is not

A checkpoint file is explicitly **safe to lose** — it is an
operator-visible progress export, not the crash-recovery mechanism. The
canonical recovery state Bernstein uses to resume after a stop or crash is
a separate structure (`Checkpoint` / write-ahead log), unrelated to this
command's output.

This command is also unrelated to
[checkpointed retries](checkpointed-retries.md), which is about whether a
*failed task's retry* can resume the adapter's native session (warm/fork/cold)
— a per-task recovery decision, not a session-wide progress snapshot.

## Source

`src/bernstein/cli/commands/checkpoint_cmd.py`,
`src/bernstein/core/persistence/session.py` (`CheckpointState` alias,
`save_checkpoint`), `src/bernstein/core/persistence/checkpoint.py`
(`PartialState`).
