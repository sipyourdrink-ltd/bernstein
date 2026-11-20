## Fail detached SSH tasks on a nonzero remote exit

Detached SSH execution now records `task.failed` with the remote exit code
when a task command fails, instead of recording `task.completed`. The SSH
receipt retains the exit code, and later tasks stay pending. Both the supervisor
and the SSH goal helper close the run as failed. Restarting the supervisor
restores a failed result if it was interrupted after the journal closed.
Concurrent finalizers reuse the first journal outcome and its terminal receipt.
The known limitations also clarify that the
detached local path without an SSH sidecar records task lifecycle events but
does not yet execute the submitted task.
