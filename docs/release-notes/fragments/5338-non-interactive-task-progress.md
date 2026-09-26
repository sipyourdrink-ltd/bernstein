## Non-interactive run output shows planned and first-task state transitions

In non-interactive mode (CI or schedulers without a TTY), `bernstein run` now prints one line per task state transition it observes in the brief window before detach: `task <id> <state> adapter=<name> model=<route> title="<first 60 chars>"`, along with a `planned` line the first time a task is seen in a non-terminal state. On a goal-driven run this window typically contains the planner's decompose task, so the output covers planning and first-task transitions rather than a full per-task execution log.

Additionally, `bernstein status` now renders `Adapter` and `Model` columns in the interactive task table, and the `--json` route carries the same fields for non-TTY consumers (#5338). Unrecorded routing is shown as `unknown` rather than a fabricated default.
