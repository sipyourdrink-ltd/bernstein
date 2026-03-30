# N65 — Spend Forecasting

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Teams cannot predict upcoming AI costs and are caught off guard by budget overruns because there is no way to project future spend based on current trends.

## Solution
- Implement `bernstein cost forecast`
- Analyze the last 30 days of daily cost data from `.sdd/` cost records
- Apply simple linear regression to project next month's spend
- Display predicted spend with a confidence interval
- Warn if the forecast trends over the configured budget threshold
- Show a simple ASCII chart of historical trend and projection

## Acceptance
- [ ] `bernstein cost forecast` outputs a projected spend for the next month
- [ ] Forecast uses last 30 days of usage data
- [ ] Simple linear regression is applied for projection
- [ ] Confidence interval is displayed alongside the prediction
- [ ] Warning is emitted if forecast exceeds configured budget
- [ ] Historical trend and projection are visualized in CLI output
