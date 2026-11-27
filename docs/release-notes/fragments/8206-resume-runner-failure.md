## A resume whose runner raised is recorded as failed

A resume request whose runner raised an exception was recorded `resumed`, so
`bernstein resume` and `bernstein ledger resume` reported
`runtime_acknowledged: true` for an attempt that failed, and the request could
never be claimed again. Both the single-task resume path and the supervisor's
ssh-runner path now record a new `failed` state. A failed request reports
`runtime_acknowledged: false`, can be claimed again by its `request_id`, and is
re-queued when the resume command is run again.
