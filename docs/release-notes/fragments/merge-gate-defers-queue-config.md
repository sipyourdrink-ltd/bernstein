## The merge-gate page stops restating the queue configuration

`docs/operations/merge-gate.md` carried its own copy of the merge-queue
provisioning payload, and nothing kept it in step with
`docs/operations/merge-queue.md`, which is the declared source of truth. It had
drifted: it told an operator to set `max_entries_to_merge=5`, a value the
runbook pins to `1` and `tests/unit/test_merge_queue_runbook_docs.py` guards,
because merging several entries in one push advances `main` by N commits under
a single push event that reports only the last SHA — so the auto-release gate
skips the version bump for every entry but the last, with green CI, no tag, no
publish and no error. `min_entries_to_merge_wait_minutes` had drifted too.

Step 2b now links to the runbook's Enable section and explains why those two
parameters are load-bearing rather than restating any values. A new test holds
the page to that: a merge-queue parameter may be named on a line that cites the
runbook, and may not be assigned anywhere else, so a second copy cannot come
back and go stale unnoticed (#5506).
