## Playbook validation and order-independent digest

Playbook parsing now rejects unknown fields and invalid clause kinds, raising `PlaybookValidationError` instead of silently ignoring them. The `principal_class` field on clauses and `principal_classes` on playbooks allow scoping ceilings to declared agent roles. `Playbook.content_hash()` now produces identical digests for playbooks that declare the same posture in a different clause order. Closes #4979.
