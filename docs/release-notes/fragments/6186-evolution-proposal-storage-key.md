## Evolution state is filed under a key that does not repeat between runs

`ProposalGenerator` mints `UPG-NNNN` from a counter that restarts in every
process, so the first proposal of every run is `UPG-0001`. The backup
directory, backup manifest and `history.jsonl` lookup were all keyed on that
label, so rolling back a proposal that never applied could restore a
different run's backup and attribute the restore to the wrong proposal.
On-disk state is now keyed on `UpgradeProposal.storage_key`: the label plus a
short sha256 over the fields that identify the proposal, `created_at`
included. `UPG-NNNN` stays the label in logs, receipt file names and each
record, and history rows and receipts now carry `storage_key` too.

Upgrading with an un-reverted backup from an earlier version: a backup at
`.sdd/upgrades/backups/UPG-NNNN/` is not found by the new key. It still counts
as an applied change, so the circuit breaker trips. A rollback refuses it with
a `RollbackError` that names the directory instead of restoring it, because
the label cannot say which proposal wrote it. Restore the files in its
`manifest.json` by hand, or rename the directory aside once the tree is known
to be correct. History rows written before this carry no key and are not read
as evidence about a later proposal (#6186).
