# 737 — Interactive Task Assignment (Click-to-Assign)

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #610

## Problem

The TUI dashboard and web dashboard are read-only — you can watch but not act. When you see a task stuck in "open" or want to force a specific task to run next, there's no way to do it without CLI commands or API calls. Users want to click a task and say "run this now" or "assign this to a specific agent type."

## Design

### TUI (Textual) — keyboard + mouse interaction

In `bernstein live`, the task list becomes interactive:

**Click/Enter on a task** → action menu:
```
Task: "Add auth middleware" (open, priority 2)
─────────────────────────────
[s] Spawn now      — force-spawn an agent for this task immediately
[p] Prioritize     — bump to priority 0 (next in queue)
[m] Change model   — pick model: haiku / sonnet / opus
[r] Change role    — pick role: backend / qa / security / docs
[c] Cancel         — cancel this task
[k] Kill agent     — kill the agent working on this (if in_progress)
[ESC] Back
```

**Spawn now** → POST `/tasks/{id}/claim` with force flag, then spawner picks it up on next tick.

**Drag-and-drop** (stretch goal) — reorder tasks by dragging in the TUI list.

### Web dashboard — mouse interaction

On the web dashboard (`bernstein dashboard`):
- Click task → same action menu as popup
- "Spawn now" button on each open task
- "Kill" button on each in_progress task
- Priority reorder via drag-and-drop

### API endpoints (new)

```
POST /tasks/{id}/force-claim   — force this task to be next
POST /tasks/{id}/prioritize    — set priority to 0
POST /tasks/{id}/cancel        — cancel a task
PATCH /tasks/{id}              — update model, role, priority
```

### Task server changes

The task server needs:
- `force_claim` flag that bypasses normal queue ordering
- `PATCH /tasks/{id}` for live editing task metadata
- Cancel propagation — if task is in_progress, signal the agent to stop

## Files to modify

- `src/bernstein/tui/app.py` (interactive task actions)
- `src/bernstein/tui/widgets.py` (action menu widget)
- `src/bernstein/core/server.py` (new endpoints)
- `src/bernstein/dashboard/templates/index.html` (interactive buttons)
- `tests/unit/test_interactive_tasks.py` (new)

## Completion signal

- Click on task in TUI shows action menu
- "Spawn now" immediately spawns an agent for selected task
- "Cancel" stops a running task
- Web dashboard has same interactive controls
- API endpoints work for programmatic access
