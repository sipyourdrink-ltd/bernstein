A task held for PR-mode approval whose approval PR could not be created now
emits a `task.approval_pr_failed` notification naming the task and the reason.
Previously the failure was a log line, so the task waited indefinitely and was
indistinguishable from one waiting on a reviewer.
