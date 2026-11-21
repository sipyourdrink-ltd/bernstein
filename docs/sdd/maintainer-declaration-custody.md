# Maintainer declaration custody

`scripts/maintainer_adoption.py` is an offline reader over accepted protected-main
ledger revisions. It validates canonical append-only records, exact subjects and
report hashes, with permanent revocation of a repository/PR/head. Git replacement
objects are disabled and incomplete history fails closed. Its trusted snapshot
must match the operator-managed origin/main ref.

The ledger path is reserved under .github maintainer ownership; this slice
installs no ledger and adds no active quorum consumer. No inference, network,
workflow trigger, privileged publication or merge authority is provided by the
reader. The operator contract is in docs/governance/maintainer-adoption.md.
