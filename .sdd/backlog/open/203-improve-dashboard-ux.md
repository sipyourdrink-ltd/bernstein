# Improve Textual dashboard UX

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** medium

## Problem
The Textual dashboard needs polish to feel truly modern:
1. Agent cards should show which specific tasks they're working on (title, not just count)
2. Need a log pane showing recent agent activity (last 20 lines from agent logs)
3. Progress bar should be more visual
4. Add keyboard shortcuts: s=stop, l=logs toggle

## Implementation
- Add a third panel: "Activity Log" — tails the most recent agent log file
- Show task titles in agent cards, not just count
- Add a RichLog widget for streaming log output
- Use Textual's built-in keybinding system

## Files
- src/bernstein/cli/dashboard.py

## Acceptance criteria
- Dashboard shows 3 panels: Agents, Tasks, Activity Log
- Agent cards show task titles
- 's' key triggers bernstein stop
- Log panel streams recent agent output
