## The seal records whether execution had actually stopped

Finalization seals the journal head into the lineage spine and writes the run
receipt. Nothing recorded whether the processes the run started had exited
first, so a tool process that outlived its wrapper could still write into a
worktree or the integration branch *after* the receipt covering the run was
produced — and the record was silent about it.

A `run_quiescence` row is now appended before the seal, carrying `verified`,
the `residual` groups by session id and pgid, the `method` used, and how many
groups were `checked`. The sealed head and the receipt therefore cover the
answer instead of being written over an unanswered question, and an operator
reading the journal can tell a quiet run from one that was sealed over work
still in flight.

The distinction the row keeps is between *checked and clean* and *could not
check*. On a platform without process groups it records
`verified: false, method: "unsupported"` — never a silent true, because
`process_group_alive` falls back to the lead pid there and would answer a
narrower question than the row claims. A check that fails outright records
`method: "check_failed"` rather than raising: a run that already completed must
not fail because its quiescence could not be established.

This records; it does not yet stop anything. Waiting out the drain timeout and
escalating with `kill_process_group_graceful` for what remains is the rest of
#5272.
