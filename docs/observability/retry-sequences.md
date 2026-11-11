# Retry sequences

`task_retry_sequences(ledger_dir, *, run_id)`
(`bernstein.core.persistence.runs_report`) answers a narrower question than
the masked-failure share: not "what fraction of runs needed a retry" but
"which task ids in *this* run failed, how many times, and did they go on to
succeed."

This is a library function with no CLI surface yet - the slice landed ahead
of its first caller so the reporting shape could be reviewed on its own.
Call it directly from Python until a `bernstein runs` subcommand wraps it.

## Return shape

One `TaskRetrySequence` row per task id that failed at least once and then
completed - a task that succeeded on its first attempt, or that never
completed, is not a masked failure and is omitted.

| Field | Type | Meaning |
|---|---|---|
| `run_id` | `str` | The ledger root this sequence was read from. |
| `task_id` | `str` | The task's stable identity within that ledger root. |
| `failed_attempts` | `int` | Total `task.failed` entries recorded before the first `task.completed`. Not necessarily consecutive - a `task.scheduled` or `task.started` entry in between neither resets nor breaks the count. |
| `succeeded` | `bool` | Always `True` for a row this function returns; the field exists so the type can represent a not-yet-succeeded task id if a future caller needs that. |
| `started_at` | `float \| None` | Unix instant of the task id's *first* `task.started` entry. `None` when no `task.started` entry exists at all - never a substitute instant from another event kind such as `task.scheduled`. A caller that gets `None` is being told the fact isn't known, not handed a plausible-looking guess. |
| `completed_at` | `float \| None` | Unix instant of the `task.completed` entry, or `None` when the task id never completed. |

Rows are ordered by `task_id` for a deterministic, byte-identical report
over the same ledger.

## Example

```python
from pathlib import Path
from bernstein.core.persistence.runs_report import task_retry_sequences
from bernstein.core.persistence.work_ledger import run_ledger_dir

ledger_dir = run_ledger_dir(Path(".sdd"), run_id="run-a")
for row in task_retry_sequences(ledger_dir, run_id="run-a"):
    print(row.task_id, row.failed_attempts, row.started_at)
```

## Relationship to masked-failure reporting

`masked_failures` (same module) reports a run-level share: of every finished
run, what fraction succeeded only after at least one retry. `task_retry_sequences`
is the per-task detail behind one run's contribution to that share - use it
when a masked run needs to be broken down by which task ids actually failed
and how many times, not just that the run as a whole needed a retry.
