## Swarm migration no longer double-spawns in-flight chunks on a re-run

Re-running `bernstein migrate` with the same `--id` while a swarm was still
executing respawned every chunk not yet marked complete — including chunks
whose task was still in flight — so two tasks could own and edit the same
files at once. `spawn_swarm` now reuses a chunk's existing task while the
server still reports it active, and only respawns when the task is gone or
terminal, preserving the #4541 guarantee that a permanently dead task id is
never returned forever (#4624).
