## A stacked queue entry is classified with the entries ahead of it

`determine-changes` classified a merge-queue run from
`github.event.merge_group.base_sha`. For a stacked entry that is the previous
entry's head, so the planner saw only that entry's own diff: a docs-only entry
behind a Python change read as `docs_only=true`, every Python test job
skipped, and `CI gate` reported green for a combination nothing had run.

The planner now diffs from the batch root, the merge-base of the queue head
with the queue's target branch. The queue squashes, so that merge-base does
not move while the group builds and every job in one run resolves the same
commit. When the root cannot be resolved the planner fails closed and runs
everything (#5901).
