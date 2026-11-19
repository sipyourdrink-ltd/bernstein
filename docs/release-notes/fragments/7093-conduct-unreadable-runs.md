## Show unreadable runs in conduct period summaries (#7093)

Add `UnreadableRun` and `UnreadableRunReason` (`absent`, `unverifiable`,
`torn`) to `ConductAggregate` and `aggregate_conduct` so supplied run IDs whose
journals could not be read appear in deterministic order alongside valid runs
with `unverified` coverage, keeping the period coverage `unverified`.
