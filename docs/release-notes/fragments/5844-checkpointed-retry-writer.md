## Ordinary crash/timeout retries can now resume warm

`checkpoint_retry.record_task_checkpoint` had exactly one production caller
(an operator's `steer.pause`), so the warm-resume machinery from
#2359/#2403 never actually fired on the crash/timeout retry path: every
retry stamped `cold`/`no_checkpoint` regardless of whether a checkpoint
would have made it resumable. `_write_retry_checkpoint` now writes that
checkpoint at the moment a dying session's adapter, session id, and
worktree are still known, mirroring `heartbeat._write_stall_checkpoint`.
Fail-open, like the stall checkpoint beside it: a write failure never
blocks the retry decision that follows. This is user-visible behavior:
some ordinary retries that previously resumed cold now resume warm.
