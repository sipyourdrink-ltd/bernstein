## Replay scorecard projects tool calls, retries, verifier coverage and approval outcomes off a sealed journal

`bernstein.core.replay.scorecard.derive_scorecard` folds a sealed run journal into an
operator-facing scorecard (tool-call and retry counts, verifier coverage, approval gates
encountered / honoured / overridden, recoveries as failed actions followed by a repaired
retry). Every number carries the event-index range it was computed from and the
projection is a pure function of the journal: no log reads, no re-execution, no
filesystem or clock access outside the journal itself, and a torn tail is reported as a
legible error rather than a silent undercount (#5402).
