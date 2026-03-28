# 621 — Cost Attribution Dashboard

**Role:** frontend
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** #601

## Problem

Cost data from the cost tracker has no visual representation. Teams cannot see which agents, tasks, or runs consume the most budget. Without a dashboard, cost governance is invisible and hard to act on.

## Design

Build a cost attribution dashboard with three views: per-agent (which agents cost the most), per-run (cost breakdown by orchestration run), and per-team (aggregate cost over time). Use a lightweight web UI served by the task server — no external dependencies beyond what ships with Python. Render charts using an embedded JavaScript charting library (Chart.js or similar) served as static assets. Include: cost timeline chart, agent cost pie chart, model usage breakdown, and cost-per-task-type analysis. Add a CSV export for finance teams. The dashboard should be screenshot-ready for marketing materials. Accessible via `bernstein dashboard` or `http://127.0.0.1:8052/dashboard`.

## Files to modify

- `src/bernstein/core/task_server.py`
- `src/bernstein/web/dashboard.py` (new)
- `src/bernstein/web/static/` (new — JS/CSS assets)
- `src/bernstein/web/templates/dashboard.html` (new)
- `src/bernstein/cli/dashboard.py` (new)

## Completion signal

- `bernstein dashboard` opens a web UI with cost charts
- Per-agent, per-run, and per-team views functional
- CSV export works for cost data
