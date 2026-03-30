# P78 — Team Plan

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Teams need unlimited usage, priority model access, and centralized identity management, but there is no paid tier to offer these features or collect revenue.

## Solution
- Define team plan: unlimited tasks, priority model routing, SSO support, $49/seat/month
- Integrate Stripe for subscription billing: create `Product` and `Price` objects, handle `checkout.session.completed` and `invoice.paid` webhooks
- Implement feature flags system (`plan:team:unlimited_tasks`, `plan:team:priority_routing`, `plan:team:sso`) stored in user/org record
- Gate features in middleware by checking active plan and feature flags
- Add SSO via OIDC integration (Google Workspace, Okta) for team plan orgs
- Build billing management page: current plan, seats, invoices, upgrade/downgrade

## Acceptance
- [ ] Stripe integration creates subscriptions at $49/seat/month
- [ ] Webhook handlers process `checkout.session.completed` and `invoice.paid`
- [ ] Feature flags gate unlimited tasks, priority routing, and SSO for team plan
- [ ] SSO login works via OIDC for team plan organizations
- [ ] Billing page shows current plan, seat count, and invoice history
- [ ] Downgrading to free tier re-applies free tier limits immediately
