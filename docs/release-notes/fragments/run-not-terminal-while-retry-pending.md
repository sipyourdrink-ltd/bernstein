## A failed task no longer ends the run before its retry exists

`bernstein run` believed a single quiescent poll. A failed task is not the end
of a run - the orchestrator's retry sweep reads the snapshot taken at the top
of the tick, so a task failed later in that same tick is first offered to
`maybe_retry_task` on the next one. For the whole of that gap the board holds
nothing open, nothing claimed and no live agent, which is indistinguishable
from a finished run: the CLI printed its summary, signalled shutdown and
exited while the run carried on retrying for another half hour.

A quiescent board that still holds a failed task is now re-observed across a
45 second window before it is believed. A run whose tasks all succeeded has no
retry to wait for and still exits on the first observation, and a run whose
failed task has exhausted its retries waits out the window once and then
exits. The check reads the `failed` count from the full histogram when it is
available and from `/status` when it is not, so it holds on the poll where the
original defect fired.

(#5622)
