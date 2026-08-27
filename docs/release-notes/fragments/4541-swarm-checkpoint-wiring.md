## Swarm migration checkpoints now record chunk completion

`bernstein migrate` checkpoints previously never learned that a chunk task
had finished: `mark_chunk_complete` and `reduce_swarm` had no caller, so a
restarted migration re-spawned every chunk from task-id memory instead of
verified completion, and the reduce step that aggregates the swarm's pass/
fail outcome could never run. Chunk completions and permanent failures are
now recorded in the plan's checkpoint as each chunk task resolves; once every
chunk has resolved, the aggregate report runs automatically and is posted to
the bulletin board (#4541).
