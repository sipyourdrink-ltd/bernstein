## A rollback that restores nothing no longer reports success

`FileUpgradeExecutor.rollback_upgrade` restored files from an in-memory
dictionary populated during the same process that applied the change. In any
other process that dictionary was empty — so the loop iterated zero times and
returned `True`, and a rollback that restored nothing was indistinguishable
from one that worked. It also ignored its own argument, so it could not roll
back a named proposal even in the process that applied one, and it recorded
nothing, so after the process exited there was no answer to "was this rolled
back, and what did that restore".

Rollback is now driven by the proposal and by a record on disk. When an apply
backs a file up with `_backup_file`, the copy goes under
`upgrades/backups/<proposal id>/` with a manifest beside it, and a rollback
reads that manifest instead of process memory. No shipped category writes such
a backup yet — every category currently resolves to a no-sink skip — so in this
release a rollback always finds nothing applied and says so. The restore path
is groundwork for the first category that mutates files; it is covered by tests
that drive a sink written the way a real one would be. Rolling back one
proposal does not touch another's files, and a manifest entry or proposal id
that would resolve outside `upgrades/` or the config directory is refused.

The three outcomes are kept distinct: nothing was applied, so there is nothing
to undo; everything in the manifest was restored; or the restore could not be
performed, which now raises `RollbackError` instead of returning `True`. That
includes a failure to record a rollback after the files were already restored.
The evolution loop re-raises rather than applying the next proposal on top of a
tree in an undeclared state, and `EvolutionCoordinator.execute_pending_upgrades`
marks the proposal rejected and stops the pass.

Every rollback writes a receipt to `upgrades/rollbacks/<proposal id>.json` with
the files restored and a sha256 over the receipt body's canonical JSON, the
same shape the change-contract verdict receipt uses. The hash is keyless: it
catches accidental corruption, not an editor who recomputes it. A repeat
rollback or a reused proposal id writes `<proposal id>.<n>.json` instead of
overwriting the earlier receipt.
