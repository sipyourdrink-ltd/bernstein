## `BERNSTEIN_LOCAL_ONLY` keeps a run's merge-backs off the remote

A successful merge-back always pushed. `safe_push` fetches, may rebase, and
writes to `origin`, so on a repository that has a remote a local-only or offline
run published agent commits nobody had reviewed -- and the only way to prevent
it was to remove the remote. Set `BERNSTEIN_LOCAL_ONLY=1` and the merge still
happens while the push after it is skipped outright, with no remote I/O for it.
The pending-push retry queue is skipped too, and left intact so turning the flag
off resumes it.

The flag covers the merge-back push only. Two other paths still reach the
remote when it is set: the salvage push of a `salvage/<id>` branch when a dirty
worktree is captured, and evolve mode's push to `main` (#5961).
