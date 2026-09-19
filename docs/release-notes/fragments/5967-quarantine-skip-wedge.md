Fix quarantined task skip wedging quiescence (#5967)

When a quarantined task is skipped in claim_and_spawn_batches, it now transitions to TaskStatus.FAILED instead of being left open with a bare continue. This ensures the orchestrator's _raw_open count reaches zero and quiescence can be achieved.
