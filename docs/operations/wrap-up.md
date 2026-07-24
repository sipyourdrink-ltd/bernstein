# Session wrap-up

`bernstein wrap-up` ends the current session with a structured brief:
completed tasks, learnings pulled from failed tasks, a git diff summary of
everything changed this session, and a prioritized list of what's left. The
brief is written to `.sdd/sessions/<timestamp>-wrapup.json` and printed as a
Rich-formatted summary.

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
| `git_diff_stat` | `git diff --stat` from the session's starting commit to `HEAD` (falls back to uncommitted changes vs `HEAD` if the start commit can't be located) |
| `completed_task_ids` | IDs of `done` tasks, so a later PR-open step can link each task's verification-evidence bundle |

The session start commit is estimated by reading `saved_at` from
`.sdd/runtime/session.json` and locating the oldest commit made after that
timestamp; the diff is computed against that commit's parent.

## `--stop` behaviour

When `--stop` is passed, after the brief is saved the command runs a
graceful drain (`DrainCoordinator`) with a wait timeout of `--timeout`
seconds, giving in-flight agents a chance to reach a clean stopping point
before the orchestrator halts.

## Source

`src/bernstein/cli/commands/wrap_up_cmd.py`,
`src/bernstein/core/persistence/session.py` (`WrapUpBrief`, `save_wrapup`),
`src/bernstein/cli/commands/stop_cmd.py` (`soft_stop`, used by `--stop`).
