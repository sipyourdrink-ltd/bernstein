## Record salvage merge to run journal and track merge commit

When an orphaned or crashed agent's partial work is salvaged, the successful merge
is now recorded as a `task_merged` event in the run journal (including `merge_commit`,
`salvaged_commit`, and `reason`) before task retry events occur. Ordinary `task_merged`
events also now capture the resulting `merge_commit` SHA on the integration branch (#5271).
