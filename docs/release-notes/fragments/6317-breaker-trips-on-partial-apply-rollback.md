## Circuit breaker now trips when a partial apply is rolled back

After #5913 the evolution loop recorded a rollback in the circuit breaker only
when `was_applied` was true, but nothing ever wrote an `applied` history row, so
a rollback that reverted a half-applied change left the breaker closed (#6317).

`FileUpgradeExecutor.was_applied` now also treats the backup manifest as
evidence, since `_backup_file` writes it before any file is mutated. A partial
apply that gets reverted trips the breaker; a failure that changed nothing still
does not, and a proposal that has already been rolled back reads as not applied.
