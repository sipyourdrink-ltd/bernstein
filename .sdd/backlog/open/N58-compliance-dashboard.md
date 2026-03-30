# N58 — Compliance Dashboard

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Compliance officers have no real-time visibility into policy adherence — they must manually run reports and cross-reference logs to identify violations and evidence gaps.

## Solution
- Add a compliance page to the web dashboard showing: active policies, recent violations, evidence gaps, and remediation suggestions
- Implement real-time updates via Server-Sent Events (SSE) so the dashboard reflects changes without page refresh
- Pull compliance data from audit logs, policy configs, and run artifacts
- Provide filtering by policy type, severity, and date range
- Show remediation suggestions with links to relevant documentation

## Acceptance
- [ ] Web dashboard has a dedicated compliance page
- [ ] Dashboard displays active policies with their current status
- [ ] Dashboard displays recent violations with severity and timestamp
- [ ] Dashboard highlights evidence gaps requiring attention
- [ ] Dashboard shows remediation suggestions for each issue
- [ ] Real-time updates work via SSE without page refresh
