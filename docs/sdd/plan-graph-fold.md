# Structural plan graph replay

`bernstein.core.replay.fold_plan_graph(events)` derives the latest
recorded structural graph from an append-ordered, in-memory journal prefix.
The replay package exports both `fold_plan_graph` and `PlanGraphResult` alongside
its existing journal and replay helpers. The implementation remains available
at `bernstein.core.replay.plan_graph`.
This is the first fold-only slice of #3614. The scheduler still reads its
existing stored plan; amendments and a command-line interface are deferred.

The input uses current `plan.graph` and `plan.graph.full` journal rows. The
fold reuses `TaskNode`, `SCHEDULE_PROJECTION_REV`, `canonical_graph_bytes` and
`canonical_graph_digest` from `orchestration.schedule_projection`. Node ids,
roles and sorted dependencies carry identity; title, description and metadata
are empty in the returned structural nodes, exactly as the producer hashes
them. Goals, labels, timestamps and unrelated events do not change the result.
The fold preserves dangling and repeated dependencies and imposes no extra
DAG or cycle rule. Duplicate node ids are invalid: TaskStore keys tasks by id.

The frozen `PlanGraphResult` contains `status`, `nodes`, `recorded_digest`,
`recomputed_digest`, `canonical_bytes` and `reason`. Only an available result
exposes nodes and canonical graph bytes; those bytes hash to both digests.

| Status | Meaning |
| --- | --- |
| `empty` | No graph event in the prefix; no graph bytes or digests. |
| `available` | A full snapshot reproduces its recorded structural digest. A zero-node graph is available too. |
| `unavailable` | The latest digest marker has no matching latest full snapshot; no stale graph is returned. |
| `invalid` | A relevant row has an unsupported revision, malformed fields, duplicate ids, inconsistent count or digest mismatch. |

The producer writes digest markers and full snapshots independently, retaining
separate retry state after append failure. A valid full snapshot therefore
supersedes an older marker even if their digests differ; a full-only prefix is
also sufficient. A delayed marker matching the latest full snapshot reuses
that snapshot. A newer unmatched marker hides the snapshot until a valid full
row is available. Every truncated producer prefix returns a result. An invalid
relevant row anywhere makes the supplied prefix invalid, including when a
later row is valid. Invalid results expose a diagnostic reason and, for digest
mismatches, the recorded and recomputed digests.

This is payload consistency checking, not journal authentication. Callers must
verify journal chain integrity, coverage and any independently trusted sealed
head separately. The fold neither reads files nor verifies external identity.

Generated tests run the actual Orchestrator producer methods against a real
EventJournal, including independent append failures and every prefix. Separate
Python processes with different hash seeds fold the same generated prefixes
and compare byte-identical output. Existing producer and review-board paths
remain unchanged.
