# Claim-admission API

For an explicitly configured single-process TaskStore controller:

| Method | Path | Request | Result |
| --- | --- | --- | --- |
| GET | `/claim-admission` | none | Current scoped operator state |
| POST | `/claim-admission/enroll` | `{"reason":"...", "expected_revision":0}` | Explicit initial enrollment, halted |
| POST | `/claim-admission/halt` | `{"reason":"...", "expected_revision":0}` | Persistent halt of new claims |
| POST | `/claim-admission/resume` | `{"reason":"...", "expected_revision":0}` | Resume from valid existing history |

All are mirrored under `/api/v1`. An authenticated operator is required;
request bodies cannot select actor, store, controller or trust roots. Reasons
are trimmed, nonempty and at most 2000 characters; extra fields are rejected.
An unconfigured controller returns 409, unavailable or invalid persistent state
returns 503, and a principal without operator authority returns 403.

A successful response contains `scope` (`task-claim-admission`), `controller_id`,
`halted`, `revision`, `actor` and `reason`. These are operator state, not worker
lease or global execution-stop evidence. See [coverage and persistence
limitations](../operations/claim-admission.md).

Every transition requires `expected_revision`: use zero for initial enrollment and
the current GET revision for halt/resume. A stale request refuses instead of
reopening a newer halt. Fetch state again before another deliberate action.
