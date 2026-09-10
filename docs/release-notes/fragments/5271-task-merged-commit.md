## The ordinary `task_merged` row now names the commit it produced

Neither `MergeResult` nor the `task_merged` journal row it feeds could be
tied back to a git object -- the row said a task was merged, but not which
commit that was. `MergeResult` now carries `merge_commit`, read via
`git rev-parse HEAD` right after a successful merge commit, and
`record_task_merged` accepts an optional `merge_commit` that lands on the
row when one exists. A merge that found nothing to commit (branches
already identical) records no `merge_commit`, rather than fabricating one
(#5271, slice 1 of 2 -- the salvage-merge path for a crashed agent's
partial work is a separate follow-up).
