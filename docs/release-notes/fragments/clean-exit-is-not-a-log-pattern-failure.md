## An agent that exited 0 is no longer failed for what it printed

The orphan path greps a dead agent's transcript for failure signatures
("401", "rate limit", "timeout") and fast-fails the task on a hit. It ran for
every orphaned session, exit-0 sessions included, so an agent that merely
mentioned one of those strings was treated as an agent that died of it. On
2026-09-02 a task whose work had already merged was failed with
`auth_error detected in agent log (exit_code=0)`, retried twice and sent to
the DLQ, because the agent's final message quoted an `HTTP 401` it had
already handled. Tightening the patterns could not close this on its own
(#2183): an error word on the same line is exactly what a report about an
error looks like.

A process that exited 0 did not die of anything, so its transcript is no
longer consulted; the session goes to the ordinary orphan recovery path,
which decides on commits, the completion data and the fast-exit probe. A
session reaped without an exit status is unchanged and still classified from
its log.

(#5618)
