## Helper scripts an agent wrote and ran survive the worktree that held them

An agent that solved a task by writing a throwaway script and executing it left
no trace of it once the worktree was collected. The run receipt recorded that
something ran; the file itself went away with the directory, so a later reader
could see the effect and never the instrument. `bernstein worktrees gc` now
content-addresses those files into the store before it destroys anything.

Which files count is decided from the run journal alone: a helper is a file the
run both created and executed, classified by `classify_run_helpers` from
`file_create` and `file_execute` events, so the set is derived rather than
guessed and two readers of the same journal agree. Paths are resolved inside the
worktree and one that escapes it is dropped rather than followed. Each captured
file appends a `run_helper_captured` row naming it in the run receipt.

Capture happens after the reap event is recorded and before the directory is
removed, and it is best-effort: a failure to capture never blocks collection, so
a full disk or an unreadable file costs the artefact rather than the sweep. A
dry run captures nothing and reports what it would have taken.
