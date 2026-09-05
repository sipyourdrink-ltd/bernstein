## A session is alive while any member of its process group is

Finalization seals the journal head into the lineage spine and writes the run
receipt once drain reports every session dead. Drain asked
`_check_alive_process`, which was `proc.poll()` on the stored wrapper alone.

Adapters spawn the tool with `start_new_session=True`, so the wrapper is a
session leader and its pid is the group id — the tool, and anything it forks,
lives in that group and outlives the wrapper. The check therefore reported a
session dead while its descendants were still running, and the receipt covering
the run was produced over execution that had not stopped. A surviving process
can still write into a worktree or the integration branch after the seal, and
the record said nothing about it.

Liveness now consults the group through `process_group_alive`, the same
primitive the kill/escalation path already uses for the same reason (#2643).
The wrapper's exit code is still recorded the moment it is known — that is a
fact about the wrapper — but the session is reported alive while the group has
members. On Windows there are no POSIX process groups and `process_group_alive`
falls back to the lead pid, so behaviour there is unchanged.

This is the liveness half of #5272. Recording a `run_quiescence` journal event
before the seal, and escalating with `kill_process_group_graceful` for what
remains after the drain timeout, are the rest of that issue.
