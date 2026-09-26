## Task-level retry sequences (masked-failure detail, slice 1 of 3)

`masked_failures()` already reported masked-failure *runs*, using
`FinishedRun.attempt_count` -- the sum of every task's `task.started`
count across the whole run. That sum can't distinguish a run where one
task retried three times from a run where three different tasks each
retried once: the specific task that failed and recovered was lost.

`task_retry_sequences(ledger_dir, *, run_id)` reads the raw, `seq`-ordered
ledger entries directly and reports per `task_id` -- the natural key for
"the same logical task retried" within one ledger root. For each task id
that failed at least once and then completed, it returns the number of
`task.failed` entries recorded before that completion, when the task
first started (`None` if the ledger has no `task.started` entry for it),
and when it completed.

This is slice 1 of #5106 (attempt counting only). Grouping the report by
the capability or adapter that owned each run, and exposing it with
`--json` plus a threshold exit code CI can gate on, are separate slices
not delivered here -- `task_retry_sequences` has no caller in `src/` yet.
