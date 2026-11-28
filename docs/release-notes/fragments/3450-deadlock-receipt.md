## Signed receipts for mailbox deadlocks

Bernstein now detects cycles in durable mailbox rendezvous waits during the
orchestrator tick and records one restart-safe, signed receipt per distinct
cycle. The receipt binds the participating tasks, the open mailbox entries,
and the latest open designated to break the cycle without automatically
cancelling tasks or resolving the waits. Part of #3450.
