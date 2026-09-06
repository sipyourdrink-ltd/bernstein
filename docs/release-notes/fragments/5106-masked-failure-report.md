## Runs that succeeded only after retrying are reported

A retry that eventually succeeds is invisible in any report that reads
only the final state: a run that failed twice and closed on the third
attempt looked identical to one that closed on its first —
`outcome=pr-opened`, with nothing saying it took three goes.
`FinishedRun.attempt_count` already carried the number and nothing read
it as a signal.

`masked_failures(runs)` reports every run that reached a SUCCESSFUL
outcome with more than one attempt, grouped by the host that ran it, with
every finished run in the window as the denominator. A run that retried
and still failed is not masked — it is already visible in every report,
which is the point. `MaskedFailureReport.exceeds(threshold)` is the gate
a CI check reads; an empty window is a zero share, so a quiet day does
not fire it (#5106).
