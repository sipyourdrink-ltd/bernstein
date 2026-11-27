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
| `completed_at` | `float \| None` | Unix instant of the `task.completed` entry. Every returned row has a recorded completion timestamp; `None` is only representable by the type for a task id that never completed, which this function omits. |

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

The two helpers report different signals from different inputs; task rows are
not a breakdown of the run-level share.

- `masked_failures(runs)` takes the supplied `FinishedRun` rows (often from
  `list_finished_runs`). A row qualifies when the run has a successful outcome
  (`pr-opened` or `no-changes`) and `attempt_count` is at least 2. It returns
  qualifying runs and their share of **all supplied rows**: the number of
  qualifying runs divided by the number of supplied finished runs (0 for an
  empty input). The run outcome and attempt count determine qualification;
  the helper does not inspect task failure events.
- `task_retry_sequences(ledger_dir, run_id=...)` takes one run's ledger and
  reads its ordered task events. It returns a row for each task id with at
  least one `task.failed` before its first `task.completed`, including the
  number of such failures and the task's recorded timestamps. It does not
  compute a run share and does not use the run's outcome or attempt count.

For example, an internal task failure-and-recovery sequence can appear in the
task ledger alongside a supplied successful `FinishedRun` with
`attempt_count == 1`. `masked_failures` omits that run, while the task helper
returns a row for a task with `task.failed` followed by `task.completed`. The
task helper uses that event sequence as its qualifying condition; it does not
require a second `task.started` event (and reports `started_at` as `None` if
none was recorded). Conversely, a run can qualify for `masked_failures` with
`attempt_count >= 2` while the selected ledger has no task id with a failure
followed by completion—for example, its task events show starts and completions
but no `task.failed`. In that case the run-level helper reports the run and
`task_retry_sequences` returns no rows for that ledger.
