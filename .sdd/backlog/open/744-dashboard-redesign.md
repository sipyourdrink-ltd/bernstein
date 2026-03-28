# 744 — Web Dashboard Redesign

**Role:** frontend
**Priority:** 2 (high)
**Scope:** large
**Depends on:** none

## Problem

The web dashboard is a single 281-line HTML file with basic htmx + Alpine.js. It works but looks dated compared to modern AI tool dashboards. A polished dashboard is the "wow" factor for demos and screenshots.

## Design

Redesign the web dashboard with patterns from modern design systems, keeping the htmx/Alpine stack (no React rewrite):

### Layout
- **Top bar**: stat cards grid — Total Tasks, Active Agents, Success Rate, Cost
- **Main area**: asymmetric 2-column (`1.5fr / 0.9fr`)
  - Left: task table with filters (search, status, role, sort)
  - Right: stacked sidebar cards (agent activity, cost breakdown, needs attention)
- **Bottom**: agent log feed with timestamps

### Design tokens (CSS custom properties)
- OKLCH color palette with light/dark mode
- 4-level label hierarchy: `--label` / `--label-secondary` / `--label-tertiary` / `--label-quaternary`
- 3 shadow elevations for cards, popovers, modals
- Apple-style easing curves for animations

### Components
- **Stat cards**: large tabular-nums value + small description
- **Status badges**: color-coded (green=done, yellow=in_progress, red=failed)
- **Empty states**: icon + title + description + CTA for empty panels
- **Skeleton loading**: shimmer placeholders matching final layout
- **Filter bar**: 5-column (search, status, role, date, sort)
- **Progress indicators**: stage-based for in-progress tasks (spawning → running → reviewing)
- **Bulk operations**: select multiple tasks → retry/cancel/export

### Interactive features
- Dark/light theme toggle
- Bulk task selection with retry/cancel
- CSV/JSON export of task data
- Click task → detail panel with agent log, diff size, cost

### Keep
- htmx for server-driven updates (no SPA)
- Alpine.js for client interactivity
- Tailwind CSS via CDN
- SSE for real-time updates

## Files to modify

- `src/bernstein/dashboard/templates/index.html` (rewrite)
- `src/bernstein/dashboard/static/` (new — CSS custom properties, icons)
- `src/bernstein/core/server.py` (add dashboard data endpoints if needed)

## Completion signal

- Dashboard has stat cards, filter bar, asymmetric layout
- Light/dark theme toggle works
- Status badges and empty states look polished
- Screenshot-worthy for README and demos
