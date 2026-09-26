## The snapshot path's git calls cannot hang the orchestrator

`git read-tree`, `git add -A` and `git write-tree` on the checkpoint route dropped out of the bounded wrapper to pass a custom `GIT_INDEX_FILE`, and lost the 30-second timeout with it. Every git call in the snapshot module now runs through one bounded wrapper that converts a timeout into `SnapshotError`, and `git add -A` gets a 300-second bound so a large repository is slow rather than killed. A git blocked on `index.lock` now fails the snapshot instead of stopping the run (#5724).
