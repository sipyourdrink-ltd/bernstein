## Ordinary crash/timeout retries now record a checkpointed-retry reference

`checkpoint_retry.record_task_checkpoint` had exactly one production caller
(an operator's `steer.pause`), so the crash/timeout retry path always
stamped `cold`/`no_checkpoint`, with no checkpoint recorded at all.
`_write_retry_checkpoint` now writes a checkpoint (adapter, workspace hash,
worktree path) at the moment a dying session's adapter and worktree are
still known, mirroring `heartbeat._write_stall_checkpoint`. Fail-open, like
the stall checkpoint beside it: a write failure never blocks the retry
decision that follows.

This does not yet resume anything warm: no adapter in this repo returns a
native session id at spawn time, so the writer records an empty
`session_id` and the retry decision still downgrades to cold
(`no_session_id`). What changed is the audit trail, not the retry behavior
an operator sees, until an adapter's `resume()` and a native session id
exist to thread through here.
