# N63 — Budget Alerts

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Teams have no early warning when AI spend is approaching or exceeding their budget, leading to surprise overages discovered only after the billing cycle ends.

## Solution
- Add `alerts:` section to bernstein.yaml with budget thresholds and webhook URLs
- Fire webhook notifications when spend hits 50%, 80%, and 100% of the configured budget
- Webhook payload includes: current spend, budget limit, percentage used, burn rate, and projected end-of-month total
- Support multiple webhook URLs for different notification channels (Slack, PagerDuty, email)
- Check budget thresholds after every task completion

## Acceptance
- [ ] `alerts:` section in bernstein.yaml accepts budget amount and webhook URLs
- [ ] Webhook fires at 50% budget threshold
- [ ] Webhook fires at 80% budget threshold
- [ ] Webhook fires at 100% budget threshold
- [ ] Payload includes current spend, budget, percentage, and burn rate
- [ ] Multiple webhook URLs are supported for parallel notification
