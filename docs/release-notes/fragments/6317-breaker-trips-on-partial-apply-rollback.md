## The circuit breaker is told about a rolled-back partial apply

After #5913 the evolution loop recorded a rollback in the circuit breaker only
when `was_applied` was true, but nothing ever wrote an `applied` history row, so
a rollback that reverted a half-applied change would have left the breaker
closed (#6317). No category in this release applies changes to files, so this
changes nothing a user can observe today; it is groundwork for the first one
that does.

`FileUpgradeExecutor.was_applied` now also treats the backup manifest as
evidence. `_backup_file` writes it before the caller mutates anything, so it
records an attempted change, not a changed file: a failure between the backup
and the write also trips the breaker, which is the fail-closed direction. A
file the apply creates has nothing to copy and leaves no manifest.

A rollback retires its manifest once the files are restored, so an earlier
rollback no longer hides a later partial apply that reuses the same proposal id
(ids restart at `UPG-0001` in every process). The loop records the rollback in
the breaker before running it, so a failure while writing the rollback receipt
or history cannot leave a restored tree with the breaker closed.
