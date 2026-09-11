## Non-interactive run output shows per-task progress: task, adapter, model, state

In non-interactive mode (CI or schedulers without a TTY), `bernstein run` now prints one line per task state transition until detach: `task <id> <state> adapter=<name> model=<route> title="<first 60 chars>"`, along with the corresponding line when a task is first planned.

Additionally, `bernstein status` now renders `Adapter` and `Model` columns in the task table, allowing operators to inspect which adapter and model route each task ran on from captured logs and status outputs (#5338).
