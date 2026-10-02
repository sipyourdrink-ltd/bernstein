Cross-task collusion check at merge admission (#5398): three invariants
(`weakened-test-covers-changed-code`, `guarded-symbol-split-removal`,
`guarded-config-flip-under-reader`) are evaluated over dependent task
pairs when a merge is admitted; any flag refuses the merge, and a
candidate set with unrecorded footprints — or an empty one — is
inconclusive and blocks. The merge admission receipt schema is bumped to
v3 with an optional signed `collusion` binding field; v1/v2 receipts
still load. A 10-case paired eval suite (5 colluding / 5 benign) under
`eval/cases/collusion/` scores the check into a signed submission bundle
with replayable per-case receipts.
