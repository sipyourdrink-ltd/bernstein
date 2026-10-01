## A run whose tasks were all archived can end

Step-8b quiescence self-stops only once some task has reached a terminal state,
and that check counted `done` and `failed` but not `closed`. A verified task is
archived out of `done` into `closed`, so a run whose tasks all completed and
were archived answered "nothing has run" on every tick: it logged quiescence
with `done 0->0, failed 0->0`, never reached the self-stop, and never journalled
`run_completed` or `run_quiescence`. `closed` now counts, and
`fetch_all_tasks` lists it among its default buckets so the key is always
present (#5968).
