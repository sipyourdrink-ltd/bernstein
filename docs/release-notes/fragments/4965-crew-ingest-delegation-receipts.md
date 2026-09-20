<!-- Release notes fragment for #4965 -->
### Ingest adapter for role-and-crew agent runtimes mapping handoffs to delegation receipts (#4965)

- **Role-and-crew ingest adapter**: Added `CrewIngestAdapter` and `CrewIngestPlugin` under `bernstein.adapters.crew_ingest`, implementing the `provide_ingest_adapter` extension point for multi-agent role-and-crew runtimes.
- **Trace normalization**: Normalizes foreign traces containing roles, tasks, tool call argument SHA-256 digests, and handoff sequences.
- **Cryptographic delegation mapping**: Maps role-to-role handoffs into HMAC-chained delegation receipts with parent reference linkage (`DelegationLedger.record_hop`).
- **Fail-closed parent verification**: Handoffs from undeclared roles, non-root handoffs without a resolvable parent, and missing parent references fail closed rather than accepting unverified roots.
- **Atomic ingestion**: Parent references are validated before the append-only ledger is touched, so a rejected trace records no receipts.
- **Idempotent ingestion**: Re-ingesting an already-recorded trace verifies the existing receipt chain and returns it without duplicating hops.

This slice is the adapter substrate only: `CrewIngestPlugin` is discoverable through
the plugin entry-point contract but is not yet wired into a CLI ingest command.
Production wiring and per-handoff task attribution are tracked as follow-up work
under #4965.
