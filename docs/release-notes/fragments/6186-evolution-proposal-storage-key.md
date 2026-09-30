## Evolution state is filed under a key that does not repeat between runs

`ProposalGenerator` mints `UPG-NNNN` from a counter that restarts in every
process, so the first proposal of every run is `UPG-0001`. The backup
directory, backup manifest, `history.jsonl` lookup and rollback receipt were
all keyed on that label. A second run's rollback receipt replaced the first
run's, and rolling back a proposal that never applied could restore a
different proposal's backup. On-disk state is now keyed on
`UpgradeProposal.storage_key`, the label plus a short sha256 over the fields
that identify the proposal, `created_at` included. `UPG-NNNN` stays the label
in logs and in each record. History rows written before this carry no key and
are not read as evidence about a later proposal (#6186).
