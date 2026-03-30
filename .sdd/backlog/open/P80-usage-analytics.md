# P80 — Usage Analytics Dashboard

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Users have no visibility into how bernstein is improving their productivity, making it hard to justify continued use or upgrades to paid plans.

## Solution
- Build a dashboard page in the web UI showing per-user productivity metrics
- Track and display: tasks completed (daily/weekly/monthly), estimated time saved, cost efficiency trend over time, most-used agents (top 5), top workflows (top 5)
- Estimate time saved by comparing task duration against baseline manual estimates stored per task type
- Store metrics in time-series format (daily aggregates) in PostgreSQL
- Render charts using a lightweight charting library (Chart.js or similar)
- Add an API endpoint `GET /analytics/summary` returning JSON for CLI integration
- Include date range filter and export-to-CSV option

## Acceptance
- [ ] Dashboard page displays tasks completed with daily/weekly/monthly views
- [ ] Time saved estimate shown based on task-type baselines
- [ ] Cost efficiency trend chart rendered over selected date range
- [ ] Most-used agents and top workflows listed with usage counts
- [ ] `GET /analytics/summary` API returns metrics as JSON
- [ ] Date range filter works correctly
- [ ] CSV export downloads current view data
