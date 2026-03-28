# 526 — Real-time web dashboard for cluster monitoring

**Role:** frontend
**Priority:** 3 (medium)
**Scope:** medium

## Problem

Bernstein has a TUI dashboard (Rich-based) that only works locally. For cluster
deployments and demos, a web dashboard would be far more accessible and impressive
for portfolio/community visibility.

## Design

### Tech stack
- FastAPI serves both API and static files (no separate frontend build)
- HTMX + Alpine.js for reactivity (minimal JS, no React/Vue overhead)
- SSE (Server-Sent Events) for real-time updates
- Tailwind CSS for styling

### Views
1. **Cluster overview**: nodes, agents, task queue depth, throughput graph
2. **Task board**: Kanban-style (open -> claimed -> done/failed)
3. **Agent view**: live agents, their current task, CPU/cost usage
4. **Evolution log**: proposals, verdicts, acceptance rate chart
5. **Cost dashboard**: spend by model, by role, cumulative
6. **Terminal**: embedded log viewer for agent output

### API additions
- `GET /dashboard` — serves HTML
- `GET /events` — SSE stream for real-time updates
- Existing task/status APIs are sufficient for data

### Demo mode
- `bernstein dashboard` opens browser to http://localhost:8052/dashboard
- Works in headless mode for screenshots/recordings
- Perfect for README GIFs and portfolio demos

## Files to create
- `src/bernstein/dashboard/` — templates, static assets
- `src/bernstein/core/server.py` — SSE endpoint, static file serving

## Completion signal
- `bernstein dashboard` opens working web UI
- Real-time task updates visible
- Looks good enough for a portfolio screenshot
