# Zero-config quickstart demo

`bernstein quickstart` runs a self-contained demo: it creates a temporary
Flask TODO API project with intentional gaps, seeds three tasks against it,
runs agents to complete them, and prints a summary — with no `bernstein.yaml`
and no prior project setup required. It exists to let a new user see
multi-agent orchestration work end to end before committing to a real
project.

For a guided walkthrough that sets up your own project instead of a
throwaway demo, see the [interactive tutorial](quickstart-tutorial.md).

## Usage

```bash
bernstein quickstart                # run and clean up the temp dir
bernstein quickstart --keep         # keep the temp dir for inspection
bernstein quickstart --timeout 120  # cap orchestration wait time
bernstein quickstart --adapter codex
```

| Flag | Default | Meaning |
|---|---|---|
| `--keep` | off | Preserve the temp project directory after completion instead of deleting it. |
| `--timeout SECONDS` | 300 | Maximum seconds to wait for all seeded tasks to finish. |
| `--adapter NAME` | auto-detected | CLI adapter to drive the agents (falls back to `mock` if none is detected — no API key needed to see the flow). |

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

## Cost

Before starting, the command prints a cost estimate: roughly `$0.20` for the
3 tasks when a real adapter is detected, or `$0.00 (mock)` when none is and
it falls back to the mock adapter.

## Source

`src/bernstein/cli/commands/quickstart_cmd.py`.
