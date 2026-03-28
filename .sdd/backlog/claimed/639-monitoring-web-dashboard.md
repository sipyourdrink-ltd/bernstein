# 639 — Monitoring Web Dashboard

**Role:** frontend
**Priority:** 4 (low)
**Scope:** large
**Depends on:** #615

## Problem

There is no web-based monitoring interface for active orchestration runs. The TUI works for local use, but teams need a shareable dashboard accessible from any browser. Screenshots of a polished dashboard are essential for marketing materials.

## Design

Enhance the web dashboard (served by the task server) to provide real-time monitoring of active orchestration runs. Key views: task timeline (Gantt-style chart showing task start/end times and agent assignments), agent activity heatmap (which agents are active when), cost burn-down chart (budget remaining over time), and a live log stream. Use WebSocket for real-time updates — the task server pushes events to connected dashboard clients. Build with vanilla HTML/CSS/JS plus a lightweight charting library (Chart.js) — no React or heavy frontend framework. The dashboard must be screenshot-worthy: clean design, dark mode, professional typography. Serve static assets from the Python package.

## Files to modify

- `src/bernstein/web/dashboard.py` (enhance)
- `src/bernstein/web/static/dashboard.js` (new)
- `src/bernstein/web/static/dashboard.css` (new)
- `src/bernstein/web/templates/monitoring.html` (new)
- `src/bernstein/core/task_server.py` (add WebSocket support)

## Completion signal

- Web dashboard shows real-time task timeline and agent activity
- Cost burn-down chart updates live during a run
- Dashboard looks professional in screenshots
