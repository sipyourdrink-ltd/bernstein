# Zero-config Flask TODO demo

> **Preview.** This command does not yet complete a first run:
> all three seeded tasks fail, and the task server can die and restart mid-run.
> What it no longer does is claim otherwise — the summary reports
> `Tasks completed 0 / 3` against the seeded count and the command exits
> non-zero, so a wrapper or CI job can tell the run apart from a good one
> ([#3902](https://github.com/sipyourdrink-ltd/bernstein/issues/3902)). Plain
> `bernstein demo`
> has a separate first-run failure — a cold run can exceed the task-server
> readiness budget and exit 1 (the error says `10.0s`; the actual budget is
> 30s, see [#3905](https://github.com/sipyourdrink-ltd/bernstein/issues/3905))
> — and aborts with a `UnicodeEncodeError`
> on a Windows console using a legacy code page. Both are tracked from
> [#3825](https://github.com/sipyourdrink-ltd/bernstein/issues/3825). Use
> `bernstein init` to set up a real project in the meantime.

`bernstein demo --flask-todo` runs a self-contained demo: it creates a temporary
Flask TODO API project with intentional gaps, seeds three tasks against it,
runs agents to complete them, and prints a summary — with no `bernstein.yaml`
and no prior project setup required. It exists to let a new user see
multi-agent orchestration work end to end before committing to a real
project.

For a guided walkthrough that sets up your own project instead of a
throwaway demo, see the [interactive tutorial](quickstart-tutorial.md).

## Usage

```bash
bernstein demo --flask-todo                # run and clean up the temp dir
bernstein demo --flask-todo --keep         # keep the temp dir for inspection
bernstein demo --flask-todo --timeout 120  # cap orchestration wait time
bernstein demo --flask-todo --real --adapter codex
```

| Flag | Default | Meaning |
|---|---|---|
| `--keep` | off | Preserve the temp project directory after completion instead of deleting it. |
| `--timeout SECONDS` | 300 | Maximum seconds to wait for all seeded tasks to finish. |
| `--adapter NAME` | `mock` | CLI adapter to drive the agents. Like the rest of `bernstein demo`, real agents run only behind `--real`; without it the scenario runs on mock agents and costs nothing. |

`bernstein quickstart` remains registered as a deprecated alias for the whole
3.x line and is unregistered in 4.0.0.

## What it does

1. Creates a temp directory (`bernstein-quickstart-*`) containing a minimal
   Flask TODO API (`app.py`) that is missing input validation, 404 error
   handling, and a test suite — bundled from `examples/quickstart/` when
   present, or written inline as a fallback.
2. Seeds three backlog tasks into `.sdd/backlog/open/`:
   - input validation on `create_todo` (role: `backend`)
   - 404 handling on `update_todo`/`delete_todo` (role: `backend`)
   - a pytest suite covering the API (role: `qa`)
3. Starts a task server on port 8056 and bootstraps orchestration
   (`bootstrap_from_goal`) against those tasks.
4. Polls `/status` every 2 seconds, printing each task's completion or
   failure as it happens, until every task reaches `done`/`failed` or the
   timeout elapses.
5. Stops the server, spawner, and watchdog processes it started.
6. Prints a summary table (tasks completed, elapsed time, Python files
   produced, API cost) and, unless `--keep` is passed, deletes the temp
   directory.

## Exit code

`0` only when all three seeded tasks reached `done`. Anything else — a failed
task, an interrupted run, a bootstrap that raised — exits `1`.

The denominator in `Tasks completed` is the seeded count, not the length of the
task list the server happens to hold, so a retry that spawns a fresh task id
cannot inflate it and a torn-down server cannot deflate it to `0 / 0`. The
snapshot behind the table is taken while the server is still running; if it
could not be read, the table says `Task server unreachable` instead of quietly
reporting zeros.

## Cost

Before starting, the command prints a cost estimate. The two spellings do not
reach it the same way.

`bernstein demo --flask-todo` follows `demo`'s rule. Without `--real` it runs on
mock agents and prints `$0.00 (mock)` whatever agent CLIs are installed on the
machine. With `--real` it prints roughly `$0.20` for the 3 tasks, and if no
adapter can be resolved it stops rather than running on mock.

The deprecated `bernstein quickstart` spelling has no `--real` option and keeps
the behaviour it always had: it picks up an installed agent CLI by itself and
prints `~$0.20`, falling back to `$0.00 (mock)` only when it finds none. On a
machine with an agent CLI on PATH it therefore spends money with no flag asked
for. Pass `--adapter mock` to pin it, or move to `bernstein demo --flask-todo`.

## Source

`src/bernstein/cli/commands/quickstart_cmd.py`.
