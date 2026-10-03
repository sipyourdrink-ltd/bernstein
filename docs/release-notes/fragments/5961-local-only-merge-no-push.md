## `BERNSTEIN_LOCAL_ONLY` keeps a run's merges off the remote

A successful merge-back always pushed. `safe_push` fetches, may rebase, and
writes to `origin`, so on a repository that has a remote a local-only or offline
run published agent commits nobody had reviewed -- and the only way to prevent
it was to remove the remote. Set `BERNSTEIN_LOCAL_ONLY=1` and the merge still
happens while the push after it is skipped outright, with no remote I/O at all.
The pending-push retry queue is skipped too, and left intact so turning the flag
off resumes it (#5961).
