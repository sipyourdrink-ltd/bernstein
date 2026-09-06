## The `test` job now fetches the PR head commit, so orphan-ratchet self-checks can run

`actions/checkout`'s default `pull_request` checkout resolves the synthetic
merge commit, not the head commit that produced it. A test that needs to
inspect the real PR branch — `tests/unit/_orphan_scan.py::pull_request_head_sha`,
which compares the checked-out merge commit against
`github.event.pull_request.head.sha` — could not resolve that commit object
locally and raised `cannot inspect pull request head <sha>; fetch the PR head
before running orphan ratchets` instead of silently passing (#5565).

The `test` job now fetches the head commit by sha into `origin/pr-head`, the
same way it already fetches the base commit into `origin/pr-base` for
impacted-test selection: a sha carried on the event payload, not a branch or
PR-number ref, so every shard resolves the same commit regardless of when it
starts.
