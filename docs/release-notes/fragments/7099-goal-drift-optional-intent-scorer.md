## Connect optional intent scorer to goal-drift evaluator interface (#7099)

Add an optional `intent_scorer` parameter to `evaluate_trajectory_drift` and
`GoalDriftReplayAdapter` so an in-process fixture can supply recorded intent
scores to `DriftTrajectoryCurve.intent_scores`. Default invocations never call
a scorer, and scorer failures are recorded visibly on `intent_scorer_error`
while leaving deterministic hard measurements and threshold gates intact.
