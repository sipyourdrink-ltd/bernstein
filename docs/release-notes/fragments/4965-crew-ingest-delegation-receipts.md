<!-- Release notes fragment for #4965 -->
### Ingest adapter for role-and-crew agent runtimes mapping handoffs to delegation receipts (#4965)

- **Role-and-crew ingest adapter**: Added `CrewIngestAdapter` and `CrewIngestPlugin` under `bernstein.adapters.crew_ingest`, implementing the `provide_ingest_adapter` extension point for multi-agent role-and-crew runtimes.
- **Trace normalization**: Normalizes foreign traces containing roles, tasks, tool call argument SHA-256 digests, and handoff sequences.
- **Cryptographic delegation mapping**: Maps role-to-role handoffs into HMAC-chained delegation receipts with parent reference linkage (`DelegationLedger.record_hop`).
- **Fail-closed parent verification**: Missing or non-existent parent receipts fail closed rather than accepting unverified root handoffs.
- **Idempotent ingestion**: Re-ingesting existing traces returns verified receipts without creating duplicate delegation hops.
