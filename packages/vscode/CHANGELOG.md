# Changelog

All notable changes to the Bernstein VS Code extension will be documented in this file.

## [0.2.0] - 2026-04-05

### Added
- **Approve/reject task commands** — new context menu actions for tasks pending approval
- **Cost budget warning notifications** — toast alerts when spend exceeds configurable threshold
- **Task grouping by status** — tasks tree view now groups items by status (open, in progress, done, failed)
- **Agent tree: model badge** — agents display their assigned model in the tree view
- **Agent tree: file ownership display** — see which files each agent owns
- **Additional task status icons** — distinct icons for `pending_approval`, `orphaned`, and `waiting_for_subtasks` states

### Improved
- **Dashboard UI** — task breakdown chart, auto-refresh indicator, empty states for when no agents or tasks exist
- **Dashboard auto-refresh** — visual indicator showing when data was last updated

### Configuration
- `bernstein.showNotifications` (boolean, default `true`) — control notification toasts for task completions and failures
- `bernstein.costWarningThreshold` (number, default `80`) — percentage threshold for cost budget warnings
- `bernstein.autoStart` (boolean, default `false`) — automatically start Bernstein when opening a workspace with a `.sdd` directory

## [0.1.0] - 2026-03-29

### Added
- Initial release of Bernstein VS Code extension
- **Activity bar panel** with three views:
  - Agents tree — view current team, status, cost per agent
  - Tasks tree — view open, running, completed, and failed tasks
  - Dashboard — overview stats and alerts
- **Status bar integration** — quick view of agent count, task progress, and total cost
- **Real-time monitoring** — SSE connection to Bernstein orchestrator for live updates
- **Agent control** — kill agents, inspect logs, view output
- **Task inspection** — click tasks to view execution output and diffs
- **Dashboard browser** — open full dashboard in VS Code webview
- **Auto-connect** — automatically detects running Bernstein server on localhost:8052
- **Configuration** — customizable API URL, optional token auth, refresh interval

### Details

#### Views & Navigation
- **Agents** — shows running agents with role, model, runtime, and cumulative cost
- **Tasks** — grouped by status (open, running, done, failed) with assignment info
- **Overview** — at-a-glance stats: active agents, task completion, success rate, cost
- **Alerts** — operator notifications for failures, resource limits, and recommendations

#### Interaction Patterns
- Click agent → opens output channel with execution logs
- Click task → opens file diff or output file
- Right-click agent → context menu: Kill, Inspect, Show Logs
- Right-click task → context menu: Prioritize, Cancel, Re-assign
- Status bar click → opens dashboard

#### Performance & Reliability
- SSE connection (not polling) — minimal latency and CPU
- Debounced UI updates (max 2/second)
- Lazy-loaded webview — doesn't block VS Code startup
- Graceful offline state — "Not connected" message, no error spam
- Auto-reconnect with backoff

#### UX Polish
- Monochrome icon (no rainbow colors)
- Clean status bar — essential info only
- Tight typography — 13px text, tabular figures for numbers
- Status dots for states
- Respects VS Code light/dark theme

---

### Known Limitations
- Extension size: ~800KB (within 1MB budget)
- Webview cannot iframe localhost due to CSP — opens dashboard in browser
- No offline mode — requires running Bernstein server

### Support

Questions or issues? File a bug: [github.com/chernistry/bernstein/issues](https://github.com/chernistry/bernstein/issues)
