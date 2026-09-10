## A successful retry unblocks its dependents again

`retry_or_fail_task` mints a new task id and leaves every dependent's
`depends_on` pointing at the original, so a retry that succeeds satisfies an
edge naming an id it does not have. The task store and the DAG executor both
resolve that lineage; the orchestrator's own open-task readiness filter built a
raw id set and did not, so a retried dependency never released its dependents
and the rest of the DAG stayed open for the whole run. The fold now lives once
in `core/tasks/unreachable.py` as `satisfied_dependency_ids`, and the scheduler
agreement test covers the readiness filter as a third consumer (#4260). The
dependency validator also stops reporting a dependency as stuck while its retry
is in flight.
