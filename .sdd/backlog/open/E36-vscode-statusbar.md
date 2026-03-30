# E36 — VS Code Status Bar Extension

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Developers using VS Code have no at-a-glance visibility into Bernstein run status without switching to a terminal or browser.

## Solution
- Create a VS Code extension at `editor-plugins/vscode/` with a status bar item.
- Status bar shows current state: idle, running (with spinner), done (with checkmark), or error.
- When a run is active, show estimated cost burn next to the status.
- Clicking the status bar item opens the web dashboard URL or activates the TUI in the integrated terminal.
- Poll the Bernstein local API (localhost) for status updates every 5 seconds.

## Acceptance
- [ ] Status bar item appears when the extension is active
- [ ] Status correctly reflects idle/running/done/error states
- [ ] Cost burn is displayed during active runs
- [ ] Clicking the item opens the dashboard or TUI
