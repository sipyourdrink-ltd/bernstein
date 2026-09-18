## Skipped quarantined tasks now leave the open queue

A task quarantined with `action="skip"` was logged and dropped from the claim
batch without a status transition. The task stayed `open`, so the orchestrator
kept counting it as pending work and never reached quiescence.

Skipped quarantined tasks now fail through the normal `fail_task` path with
their quarantine reason, while `action="decompose"` tasks still decompose
first (#5967).
