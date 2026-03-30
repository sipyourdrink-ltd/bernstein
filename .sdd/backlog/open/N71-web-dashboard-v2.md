# N71 — Web Dashboard v2

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
The current web interface is minimal and lacks the depth enterprise teams need for day-to-day operations — there is no runs list, detailed task view, or cost breakdown in a browser.

## Solution
- Build a React SPA for the web dashboard using Vite + React + TailwindCSS
- Pages: runs list (sortable, filterable), run detail (task timeline, logs), task detail (inputs, outputs, cost), cost breakdown (charts), agent status (health, utilization)
- Fetch all data from the task server REST API
- Responsive layout for desktop and tablet
- Deploy as static assets served by the task server

## Acceptance
- [ ] React SPA scaffolded with Vite + React + TailwindCSS
- [ ] Runs list page with sorting and filtering
- [ ] Run detail page with task timeline and logs
- [ ] Task detail page with inputs, outputs, and cost
- [ ] Cost breakdown page with charts
- [ ] Agent status page with health and utilization indicators
- [ ] All data fetched from task server API
