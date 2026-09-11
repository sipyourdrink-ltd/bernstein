## Artefact-progress stall clock, repeated-command detector, and fan-out ceiling

Tasks now advance their forward-progress timestamp strictly on genuine artefact events (worktree file changes, test results posted, artefacts posted), rather than idle log output. Stalls are declared when tasks fail to produce artefacts within the configured threshold.

A repeated-command detector flags loops where the same command and exit code are run repeatedly within a task. The orchestrator fan-out ceiling bounds concurrent active tasks per coordinator, halving dynamically when multiple tasks stall with no progress and recording each admission decision in the audit trail (#5439).
