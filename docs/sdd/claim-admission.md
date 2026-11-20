# TaskStore claim-admission controller

`core/tasks/claim_admission.py` owns persistent, explicitly scoped operator
state. `TaskStore` owns its serialization with all three claim mutations and
passes blocking SQLite I/O to a thread while retaining the task lock through
cancellation. `core/routes/claim_admission.py` consumes authenticated operator
context, never request-supplied authority. `server_app.create_app` supplies the
controller configuration; the middleware explicitly classifies its prefix as
`admin:manage`. Controlled mode refuses the separate legacy claim-receipt path.

This is a single-process claim-admission slice. It has no lease enrollment,
process stop watchdog, tool-call interlock, grant-revocation consumer or remote
stop confirmation. See [operator contract](../operations/claim-admission.md).
