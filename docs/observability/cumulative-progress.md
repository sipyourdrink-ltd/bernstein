# Cumulative progress tracking

The Bernstein TUI (`bernstein --tui` / the Textual dashboard app) shows two
completion-percentage bars: a per-task **Progress** column in the task table,
and an aggregate run-level bar in the status bar showing how much of the
whole run is done. Both are colour-coded terminal progress bars rendered
client-side from data the task server already returns on every poll — there
is no separate "progress" API call.

## What renders where

| Surface | What it shows | Width |
|---|---|---|
| Status bar (top of the TUI) | Aggregate percentage across the whole run: `tasks_done / tasks_total * 100` | 12 chars + `NNN%` |
| Task list "Progress" column | Per-task completion percentage, or `—` if unknown | 10 chars + `NNN%` |

Bar colour reflects completion: `dim` below 30%, `yellow` from 30-59%,
`cyan` from 60-99%, `green` at 100%.

## Where the numbers come from

**Run-level (status bar):** `_compute_run_pct()` divides the `done` count by
the `total` count from the same status payload the TUI already polls
(`core/routes` status/summary fields) and passes the result to
`render_progress_bar()`. If `tasks_total` is zero, no bar is shown.

**Per-task (task list):** `TaskProgress.from_api()` reads a task's raw API
dict and computes a percentage with this priority order:

1. An explicit `progress.percentage` field, if the server sent one.
2. `completed_steps / total_steps`, if step counts are present.
3. `tests_passing / tests_total`, if test counts are present.
4. Otherwise `0.0`, and the column renders `—` instead of a bar unless
   `files_changed` or a truthy `progress` field is also present.

A task with `status == "done"` always shows 100%, regardless of the fields
above.

## Limitations

- This is a client-side rendering convenience, not a verified metric — it
  trusts whatever `progress`, `completed_steps`/`total_steps`, or
  `tests_passing`/`tests_total` fields the task server puts in the API
  response for a task. It is a different mechanism from the chain-computed,
  hash-verifiable per-task progress vector described in
  [Task artifacts and chain-computed progress](task-artifacts.md) — the two
  can disagree, and this page's numbers are not independently verifiable.
- `render_progress_summary()` and `render_task_progress()` (aggregate-across-
  tasks and compact single-task variants) are defined in the same module but
  are not currently called by any TUI screen — the only bars an operator
  actually sees are the two described above.
- The TUI's `_task_progresses` list (built once per poll in `app.py`) is
  populated but not currently read by any renderer; it has no visible effect
  today.

## Source

- `src/bernstein/tui/progress_bar.py` — `TaskProgress`, `render_progress_bar`,
  `render_progress_bar_text`, `render_task_progress`, `render_progress_summary`
- `src/bernstein/tui/status_bar.py` — aggregate run-level bar in the status bar
- `src/bernstein/tui/task_list.py` — per-task "Progress" column
- `src/bernstein/tui/app.py` — `_compute_run_pct`, polling loop that feeds both
