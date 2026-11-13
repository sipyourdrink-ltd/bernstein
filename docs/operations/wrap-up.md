# Session wrap-up

`bernstein wrap-up` saves a structured handoff brief: completed tasks,
learnings pulled from failed tasks, a git diff summary of everything changed
this session, and a prioritized list of what's left. The brief is written to
`.sdd/sessions/<timestamp>-wrapup.json` and printed as a Rich-formatted
summary. By default, saving the brief does not end or stop the run; pass
`--stop` to initiate a graceful agent drain after the brief has been saved.
Choose wrap-up when preparing a handoff to a later session. For a current
task-progress snapshot to refer back to during the run, choose
[`bernstein checkpoint`](checkpoint.md) instead.

## Usage

```bash
bernstein wrap-up            # save the brief, keep the orchestrator running
bernstein wrap-up --stop     # save the brief, then soft-stop
```

| Flag | Default | Meaning |
|---|---|---|
| `--stop` | off | Also perform a soft stop (graceful agent drain) after saving the brief. |
| `--timeout SECONDS` | 30 | Soft-stop drain timeout; only used with `--stop`. |

The command requires a reachable Bernstein task server (it queries `/tasks`
for task status).

## What gets captured

| Field | How it's built |
|---|---|
| `changes_summary` | One line per `done` task, using the task's `result_summary` if present |
| `learnings` | One line per `failed` task, quoting its `result_summary` (or noting no reason was recorded) |
| `next_session_brief` | Up to 10 `open` tasks sorted by priority; suggests `bernstein evolve` if none remain |
| `git_diff_stat` | `git diff --stat` from the estimated session-start commit to `HEAD`; if that start commit cannot be found or that diff has no usable output, falls back to `git diff --stat HEAD` for uncommitted changes |
| `completed_task_ids` | IDs of `done` tasks, so a later PR-open step can link each task's verification-evidence bundle |

The **Next session** section includes only tasks whose status is `open`. It sorts
by numeric priority in ascending order (the smaller number comes first); a task
with no priority is treated as priority 2. Tasks with equal priorities keep
their input order. At most 10 task rows are shown. If there are more, the brief
adds a line reporting how many remain beyond those 10. A non-empty role is
shown after the title in parentheses; a task without a role has no role suffix.
For example, rows for priorities 1, 2, and 3 (with 12 open tasks total) can
look like this:

```text
Remaining open tasks (by priority):
  [1] Fix login (backend)
  [2] Update help text
  [3] Add report export (analytics)
  [3] Add CSV export
  [3] Improve report filters
  [3] Add report tests
  [3] Document report API
  [3] Add report metrics
  [3] Review report permissions
  [3] Polish report layout
  … and 2 more
```

When there are no open tasks, the section instead says:

```text
No open tasks remaining. Consider running `bernstein evolve` to generate new work.
```

When there are no completed (`done`) tasks, `changes_summary` uses the most
recent session summary's `changes_summary` when available; otherwise it says
`No tasks completed this session.`

The session start commit is estimated from `saved_at` in
`.sdd/runtime/session.json`: the command locates commits made after that
timestamp and uses the parent of the oldest such commit as the start. It
computes the diff against that commit and `HEAD`. If no usable start commit
or start-to-`HEAD` stat is available, it falls back to the uncommitted diff
against `HEAD`.

## `--stop` behaviour

When `--stop` is passed, after the brief is saved the command runs a
graceful drain (`DrainCoordinator`) with a wait timeout of `--timeout`
seconds, giving in-flight agents a chance to reach a clean stopping point
before the orchestrator halts.

## Source

`src/bernstein/cli/commands/wrap_up_cmd.py`,
`src/bernstein/core/persistence/session.py` (`WrapUpBrief`, `save_wrapup`),
`src/bernstein/cli/commands/stop_cmd.py` (`soft_stop`, used by `--stop`).
