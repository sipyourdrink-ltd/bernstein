## Count explicit cumulative cost increases accurately

`CostTracker.record_cumulative` now charges the increase in an explicit
cumulative cost, including corrections with unchanged token counts. Duplicate
or lower reports do not add spend or refund it. Reports without an explicit
cost retain token-based delta pricing, and recorded spend continues to drive
the existing budget stop.
