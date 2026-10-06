## The merge queue runs the impacted tests, not the whole unit suite

A merge-queue group ran every `tests/unit/**` file on eight shards, while a pull
request ran only the files its change could affect. The queue now uses the
same `plan-affected-tests` planner. It selects against the batch root, the
merge-base of the default branch and the queue head, resolved once in the
planner so every shard reads the same list. It does not use
`merge_group.base_sha`, which for a stacked entry is the previous entry's merge
commit and would never test one entry's tests against another entry's change.
If the root cannot be resolved, no plan is uploaded and every shard runs the
full list as before. The whole directory still runs on the post-merge cadence
dispatch and on release pushes (#5793).
