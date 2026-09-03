## Cost warning dedup and unpriced estimate display

`price_model_usage` now warns once per distinct unpriced model name per process instead of on every call, so gateway aliases like `fleet-live` no longer spam the log and bury goal, plan, and task-state lines. Cost-estimate lines (`Estimated cost:`, `Cost estimate:`) now show `unpriced` for such models instead of `$0.00`, while priced models and legitimately free (`:free`) routes are unchanged. Token metering is untouched — unpriced calls still report their tokens in totals.
