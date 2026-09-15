## A rollback that restores nothing no longer reports success

`FileUpgradeExecutor.rollback_upgrade` restored files from an in-memory
dictionary populated during the same process that applied the change. In any
other process that dictionary was empty — so the loop iterated zero times and
returned `True`, and a rollback that restored nothing was indistinguishable
from one that worked. It also ignored its own argument, so it could not roll
back a named proposal even in the process that applied one, and it recorded
nothing, so after the process exited there was no answer to "was this rolled
back, and what did that restore".

Rollback is now driven by the proposal and by a record on disk. Each file is
copied aside under `upgrades/backups/<proposal id>/` with a manifest beside it,
so a rollback works after a restart and applies to the proposal it is handed.
Rolling back one proposal no longer touches another's files.

The three outcomes are kept distinct: nothing was applied, so there is nothing
to undo; everything in the manifest was restored; or the restore could not be
performed, which now raises `RollbackError` instead of returning `True`. The
evolution loop re-raises rather than applying the next proposal on top of a
tree in an undeclared state.

Every rollback writes a receipt to `upgrades/rollbacks/<proposal id>.json` with
the files restored and a sha256 over the receipt body's canonical JSON, the
same shape the change-contract verdict receipt uses.
