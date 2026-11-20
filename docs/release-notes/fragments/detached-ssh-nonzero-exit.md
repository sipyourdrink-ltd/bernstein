## Fail detached SSH tasks on a nonzero remote exit

Detached SSH execution now records `task.failed` with the remote exit code
when a task command fails, instead of recording `task.completed`. The SSH
receipt retains the exit code. The known limitations also clarify that the
detached local path without an SSH sidecar records task lifecycle events but
does not yet execute the submitted task.
