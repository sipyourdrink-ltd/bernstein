Salvage merges now append a chained `task_salvage_merged` journal row with the
merge and `[WIP]` commit SHAs and the crash-recovery reason. The row is written
before any later `task_retried` row and is projected into the review board's
merged column alongside ordinary merges.
