# Persistent task-claim admission

A task server can opt into one persistent claim-admission controller. This
controls **new TaskStore claims only**. It is not an installation-wide execution
halt: it does not stop running processes, issue worker leases, revoke grants or
guard tool calls, approval delivery, MCP calls or remote submissions.

## Configure and enroll

The operator creates a private directory outside agent-writable worktrees and
passes its absolute database path and a controller id to the app factory:

```python
from pathlib import Path
from bernstein.core.server import create_app

app = create_app(
    jsonl_path=Path("/var/lib/bernstein/tasks.jsonl"),
    claim_control_path=Path("/var/lib/bernstein-control/claims.sqlite"),
    claim_controller_id="installation-a",
)
```

The directory must already exist, belong to the server operator, and on POSIX
have no group or other permissions. The database is created with mode `0600`.
On Windows, the operator must provide equivalent directory and file ACLs; the
POSIX mode checks do not establish Windows containment. This is an HTTP
operator authority boundary, not protection from another process running as the
same OS user. The database path and controller id are startup configuration,
never request parameters. The database also binds the canonical task-store
path; changing stores requires separate explicit enrollment.

The existing single-process server restriction still applies. Do not run two
controllers or server processes against the same task store. Omitting both
configuration arguments retains existing behavior. Configuring only one is an
error. Configuration is explicit at the app factory; the standard CLI does not
select a controller automatically.

Missing state refuses all new claims. An authenticated operator explicitly
posts `{"reason": "Initial installation enrollment", "expected_revision": 0}` to
`/claim-admission/enroll`. Enrollment starts **halted**. Then post a nonempty
reason to `/claim-admission/resume` to permit claims. Use
`/claim-admission/halt` to stop further claims. `GET /claim-admission` reports
scope `task-claim-admission`, controller id, halted state, revision, actor and
reason. The same endpoints are available under `/api/v1`.

Mutations require the existing `admin:manage` operator context: an authenticated
SSO administrator or the configured installation operator bearer. Agent and
cluster credentials cannot act as operators; disabling authentication does not
create operator authority for these endpoints. The server derives the actor
from authenticated context; request bodies accept only the reason.

## Persistence and coverage

Halt, resume and enrollment transitions are stored in a durable SQLite history.
A halt and every TaskStore `claim_next`, `claim_by_id` and `claim_batch` operation
share the store lock. A claim already committing before halt may complete; once
halt returns successfully, subsequent claims refuse before task mutation.
Cancelling a control request does not release the lock while its durable write
is still running. Halt survives restarting with the same configured store and
controller.

Missing, unreadable, malformed or mismatched history refuses admission.
Ordinary resume never clears or initializes corrupt history; preserve the
original database for separate operator recovery. State transitions are durable
operator control records, not cryptographically sealed global halt receipts.
Do not treat restoring an old database copy as a supported resume procedure.

The legacy `/tasks/claim-receipt` endpoint is deliberately unavailable in
controlled mode because its JSON backlog path has a separate mutation lock.
Use the TaskStore claim routes. Drain cancellation does not clear this
controller's halt. Workers are not enrolled in a lease or stop watchdog by this
feature, and paths outside this configured store are not protected by it.

Every transition requires `expected_revision`: use zero for initial enrollment and
the current GET revision for halt/resume. A stale request refuses instead of
reopening a newer halt. Fetch state again before another deliberate action.
