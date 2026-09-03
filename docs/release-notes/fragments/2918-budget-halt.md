## Budget Halt Emission to Audit Chain (Issue #2918)

Bernstein now emits a `cost.budget_halt` event to the audit chain the first time a run's spend ledger crosses its soft or hard budget cap. Previously this information survived only as a ``logger.warning`` line in ``stderr``, so "this run stopped because of its budget" was not reconstructable from the tamper-evident chain.

Key capabilities:

- **Chain-anchored budget halts**: The first soft cap and first hard cap transition each append a ``cost.budget_halt`` event to the audit chain, with the band (`"soft"` or `"hard"`), spend and cap as integer nano-USD, and the previous chain digest. Operators can query ``chain.query(event_type=EVENT_BUDGET_HALT, resource_id=run_id)`` to reconstruct whether a run stopped due to budget.

- **Exactly-once emission**: The ``_halt_lock`` in ``SpendLedger`` ensures that two racing writers do not both produce a halt receipt for the same cap transition — the event is appended at most once per cap type per run.

- **Integer nano-USD payload**: Money is recorded as exact integers of nano-USD via ``nano_usd_from_float``, so the signed payload is independent of floating-point aggregation order and can be verified byte-identically across recomputations.

- **Best-effort append**: If the chain append fails, the error is logged but the ledger continues functioning without the event — the ledger is not allowed to take the orchestrator down.

- **Unattached ledgers unchanged**: When no audit chain is attached, the ledger behaves exactly as it did before: soft/halt flags are set on the status but no chain events are emitted.

- **Distinct bands**: The soft cap halt uses band `"soft"` and the hard cap halt uses band `"hard"`, allowing operators to distinguish which threshold was tripped.

- **Audit verification**: Each ``cost.budget_halt`` event includes ``prev_chain_digest`` so the full chain can be verified end-to-end. Editing a recorded cap after the fact causes chain verification to fail, making the halt tamper-evident.

- **Exported constant**: ``EVENT_BUDGET_HALT = "cost.budget_halt"`` is added to ``__all__`` in ``audit_chain.py`` for external query use.
