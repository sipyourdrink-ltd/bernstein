# Cumulative progress tracking

The Bernstein Textual dashboard (`bernstein live`) shows two
completion-percentage bars: a per-task **Progress** column in the task table,
and an aggregate run-level bar in the status bar showing how much of the
whole run is done. Both are colour-coded terminal progress bars rendered
client-side from data the task server already returns on every poll — there
is no separate "progress" API call.

## Which signal answers which question?

These signals are not interchangeable; choose one based on what you need to know.

| Signal | Input | What an operator can infer |
|---|---|---|
| Run completion percentage | The polled task-server status/summary counts: `tasks_done / tasks_total` | The fraction of tasks in the run marked done; it is a run-completion summary, not a measure of agent activity. |
| Task completion percentage | Each task's API fields (`progress.percentage`, step counts, or test counts, in that order); a task marked `done` is 100% | The dashboard's client-side estimate for that task's completion. It trusts the supplied fields and is not independently verified. |
| [Session-log activity](progress-watch.md) | Changes in a watched agent session log's mtime or size | The log is advancing, which indicates activity. Growth alone does not establish that work was completed. |
| [Per-agent token usage](token-progress.md) | Bytes appended to the session's `.sdd/runtime/<session_id>.tokens` sidecar, counted on orchestrator ticks | How much token usage has been recorded for the agent, and (when configured) how it compares with its task budget—not task completion. |
| [Journal-derived task progress vector](task-artifacts.md#progress-vector) | A projection of journaled work, including checkpoint references, evidence producers and results, diff/gate attempts, and task transitions | How much progress is earned by recorded work. It cannot be set by the agent, but it is distinct from the dashboard's API-derived percentage. |

## What renders where

| Surface | What it shows | Width |
|---|---|---|
| Status bar (top of the TUI) | Aggregate percentage across the whole run: `tasks_done / tasks_total * 100` | 12 chars + `NNN%` |
| Task list "Progress" column | Per-task completion percentage, or `—` if unknown | 10 chars + `NNN%` |

Bar colour reflects completion: `dim` below 30%, `yellow` from 30-59%,
`cyan` from 60-99%, `green` at 100%.

## Where the numbers come from

**Run-level (status bar):** `_compute_run_pct()` uses `summary.total` and
`summary.done` when those keys are present; otherwise it falls back to the
status payload's top-level `total` and `completed`, respectively. The run
percentage is `done / total * 100`. If the selected total is zero (or missing,
which defaults to zero), `_compute_run_pct()` returns no percentage and the
status bar shows no run-progress bar. For example, `summary: {"total": 4,
"done": 1}` gives 25%; with no summary counts, top-level `{"total": 4,
"completed": 1}` also gives 25%.

**Per-task (task list):** `TaskProgress.from_api()` computes a value using
these inputs, in priority order:

1. `progress.percentage`, if it is not `null` (including an explicit `0`).
2. `progress.completed_steps / progress.total_steps`, when total steps is
   greater than zero.
3. `tests_passing / tests_total`, when total tests is greater than zero.
4. Otherwise the computed percentage is `0`.

For test counts, the parser first looks inside the `progress` object and, if a
specific key is absent there, reads the legacy top-level `tests_passing` or
`tests_total` field. For example, `{"progress": {"completed_steps": 1,
"total_steps": 4, "tests_passing": 8, "tests_total": 10}}` uses the step
ratio and shows 25%, not the test ratio of 80%. Without usable percentage or
step counts, those test counts provide the percentage.

The table shows a dash (`—`) when the computed value is zero and neither a
truthy `progress` object nor a nonzero top-level `files_changed` count is
present. Thus a task with no progress fields, such as `{}`, shows a dash. A
top-level `{"files_changed": 2}` has no completion percentage to compute but
shows an explicit 0% bar; likewise `{"progress": {"percentage": 0}}` shows
0%. A task with `status: "done"` always shows 100%, even if an older reported
percentage says otherwise (for example, `{"status": "done", "progress":
{"percentage": 35}}` shows 100%).

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
