## Count explicit cumulative cost increases accurately

`CostTracker.record_cumulative` now charges the increase in an explicit
cumulative cost, including corrections with unchanged token counts. Duplicate
or lower reports do not add spend or refund it. Reports without an explicit
cost price each new token once and count toward the next explicit total for
that agent, task and model. New tokens appear in usage reports even when
their monetary delta is zero. Recorded spend drives the existing budget stop.

The persisted format is unchanged. For a key restored from a snapshot, its
first explicit report keeps the previous token-based proration or duplicate
suppression and seeds the monetary baseline for later reports. Exact cost
corrections spanning a restart cannot be recovered from those snapshots.
