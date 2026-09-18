## Journal retention no longer overshoots when the active run sorts first

`EventJournal._prune_old_runs` counted the active run's own directory toward
`BERNSTEIN_REPLAY_RETENTION`'s budget and then separately refused to delete
it, so a run whose id happened to sort before existing run directories (an
operator-pinned `BERNSTEIN_RUN_ID`, or a resumed older run) left the deletion
count short and more than `retention` run directories survived. The prune
walk now computes how many directories must go first and satisfies that
count from the oldest directories regardless of where the active run falls
in the sort order, so the total stays capped at `retention` in every case
(#5881).
