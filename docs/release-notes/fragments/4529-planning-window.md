## A failed plan no longer runs out the clock

A run whose planning task failed kept ticking with an empty ledger until it hit
its wall-clock timeout, so a fast failure cost the same wall time as real work.
The orchestrator now bounds the window it will wait for a first task and closes
the run when that window passes with nothing planned (#4529).
