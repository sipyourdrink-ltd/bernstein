## `bernstein lineage plan` renders a run's whole task graph from its journal

A run's decomposition was already recorded — the orchestrator appends a
`plan.graph.full` row carrying the goal and every task node with its role,
title and dependencies — but nothing read it back as a plan. Every review
surface ships per-task, so an operator approving task 7 of 20 had no rendering
of what the other 19 add up to.

`bernstein lineage plan <run-id>` prints the goal and the full graph, each task
with its role, title and the tasks it waits on; `--json` emits the same shape
for a machine reader. It reads through the projection
`core/replay/review_board.py` already uses, rather than opening a second path
to the same data.

Re-rendering the same journal produces identical bytes: the projection never
reads a row's wall-clock envelope, and ordering is a journal fact — nodes
sorted by task id, dependencies sorted within a node — not a render-time
choice. A journal with no `plan.graph.full` row is reported as "never
recorded", which is not the same claim as a run that planned nothing (#4958).
