## Caller-less-module guards report both drift directions in one run, and self-explain when possible

`test_token_orphans.py` and `test_compliance_module_reachability.py` each
carried a frozen `KNOWN_ORPHANS` snapshot compared against the tree in two
separate assertions, so a snapshot that drifted in both directions at once
(some entries newly caller-less, others no longer caller-less) passed the
first assertion and only failed the second on a later run -- and the failure
message read identically whether the drift came from the change under
review or from an unrelated commit that had landed on the default branch
since the snapshot was captured.

Both guards now share `tests/unit/_orphan_scan.py` and report both
directions in one message. When the environment makes it possible -- a
merge-queue run whose `HEAD` is a merge commit with enough history fetched
to resolve its second parent -- the message additionally states plainly
when the PR branch's own tree already matched the baseline, so the drift is
attributable to the default branch rather than to the change under review.
That signal is unavailable in this repository's current `Test` job (a
shallow, single-commit checkout), so it does not fire there today; the
combined single-message improvement applies unconditionally either way.
No production code changes (#5552).
