## A task that finished mid-tick is no longer reopened as an orphan

The orphan handler took its already-resolved early return from the task snapshot
fetched once at the top of the tick. By the time an orphan is judged that copy
can be seconds stale - long enough for the task to have reached `done` and had
its branch merged - so a finished task was resumed. The snapshot hit is now
re-read live before the status check, and any failure to fetch or parse degrades
to the snapshot copy, which is the previous behaviour, so the orphan path can
never abort on this.
