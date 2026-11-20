## Goal-drift benchmarks can record an in-process intent score

Python callers can pass an optional scorer to `GoalDriftReplayAdapter`.
The adapter records its score on a trajectory event for offline replay.
Scorer failures produce a warning and preserve the hard drift result.
