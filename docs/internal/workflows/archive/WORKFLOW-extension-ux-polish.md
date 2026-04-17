# WORKFLOW: Extension UX Polish Implementation

**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Deprecated
**Implements**: Task 340c — Extension Publish Pipeline + UX Polish (UX portion)

> **DEPRECATED**: This is an early draft spec superseded by the approved canonical spec.
> **Use instead**: `WORKFLOW-extension-ux.md` (v0.4, Approved)
> This file is kept for historical reference only. Do not use for implementation.

---

## Overview

This workflow specifies the implementation of "Decision-Grade Quiet Command" UI design principles for the Bernstein VS Code extension. It covers visual design (icons, colors, typography), dashboard layout, sidebar interactions, status bar display, and performance constraints. The workflow is implementation-focused: a backend architect reads this and knows exactly what UI states, interactions, and data flows must be implemented in code.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Frontend developer (Backend Architect in UX role) | Implements dashboard webview, sidebar tree views, status bar |
| Extension host process | Manages UI state, communicates with Bernstein API |
| Bernstein API (localhost:8052) | Provides real-time task/agent data via REST + SSE |
| VS Code UI system | Renders tree views, webviews, status bar |
| Cursor editor | Same VS Code API, may have slightly different rendering |

---

## Prerequisites

- VS Code extension codebase set up (src/ directory with TypeScript)
- Bernstein API server running on localhost:8052 (for testing)
- Webview UI library chosen (e.g., React, Vue, or vanilla TypeScript)
- CSS-in-JS or CSS modules for styling (no global styles)
- Test environment with a mock Bernstein API
- Design token file (colors, spacing, typography) checked into version control

---

## Trigger

Developer begins UX polish implementation after the API and core extension functionality are complete. This workflow is implemented as a series of UI components that satisfy the design and interaction specs below.

---

## Design System

### Color Palette

**Theme**: Dark-first (respects VS Code theme preference)

```
// Design tokens
neutral-950: #0a0a0a      (almost black, backgrounds)
neutral-900: #1a1a2e      (dark card backgrounds)
neutral-800: #2d2d3d      (borders, dividers)
neutral-700: #424250      (hover states)
neutral-600: #6b6b7f      (secondary text)
neutral-500: #909099      (tertiary text)
neutral-100: #f5f5f7      (primary text)

accent-700: #3730a3       (indigo, interactions)
accent-600: #4f46e5       (indigo, hover)
accent-500: #6366f1       (indigo, active)

success-600: #16a34a      (green, ✓ states)
warning-600: #ea580c      (orange, ⚠ states)
danger-600: #dc2626       (red, error states)
```

**Usage rules**:
- Text: neutral-100 on neutral-900 (high contrast, WCAG AA)
- Accents: indigo-600/indigo-700, never saturated blue
- Status colors: green/orange/red only for state (success/warning/error)
- No rainbow status indicators, no gradients
- Respect VS Code theme: use CSS custom properties to inherit theme colors where applicable

---

### Typography

```
// Font stack
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif

// Sizes and line heights
13px / 1.4 (compact, tab labels, tree items)
14px / 1.5 (body text, descriptions)
12px / 1.2 (captions, timestamps, numbers)
16px / 1.5 (titles in dashboard)

// Weight
400: normal (body)
600: semibold (labels, titles)
700: bold (never, use 600 instead)

// Features
Tabular figures: number displays (cost, task count) for alignment
```

**Usage rules**:
- Tree items: 13px, 400 weight, tight leading
- Dashboard titles: 16px, 600 weight
- Numbers (cost, count): tabular figures, monospace
- Labels: 13px, 600 weight (BUTTON states, tree item type labels)

---

### Spacing & Layout

```
4px base unit
8px: gap between inline elements (button + icon)
12px: gap between blocks (cards, sections)
16px: padding inside cards
20px: gap between major sections

Compact density: cards 80px tall, 12px spacing (not 20px)
```

---

## Workflow Tree

### STEP 1: Implement tree view data model and refresh logic
**Actor**: Frontend developer
**Action**:
- Create TypeScript interfaces for Agent and Task (mirror API responses)
- Implement polling or SSE subscription to Bernstein API
- Build tree item state management (expanded/collapsed, selected)
- Debounce UI updates to max 2 per second (prevent flickering)
**Timeout**: N/A (implementation task)
**Output on SUCCESS**:
- Tree view can fetch and display agents/tasks from API
- UI updates debounced (not live-updating on every API call)
- Tree items have proper context values for filtering menu items
- GO TO STEP 2

**Observable states during this step** (testing):
- Mock API returns 3 agents, tree displays all 3
- API returns update with 1 agent completed → tree item state changes
- Debounce prevents > 2 UI updates per second in network stress test

---

### STEP 2: Design and implement tree item rendering
**Actor**: Frontend developer
**Action**:
Render agents and tasks with this exact format:

```
▼ Agents (3)
  ● backend-abc   sonnet   $0.12   2m
  ● qa-def        sonnet   $0.08   1m
  ○ docs-ghi      flash    idle

▼ Tasks (7/12)
  ✓ Add JWT middleware
  ● Write auth tests  →  qa-def
  ○ Generate API docs
```

**Details**:
- Agent row: status dot (●/○) + name (13px) + model tag (light gray) + cost + elapsed time
- Task row: checkbox/checkmark (✓/○) + title + assigned agent (right-aligned)
- Status dot: ● (solid, active), ○ (hollow, idle)
- Colors: text neutral-100, secondary neutral-600 (cost/time)
- Icons: VS Code built-in icons (status.dot, status.ok, etc.)

**Output on SUCCESS**:
- Agent items render with correct spacing and alignment
- Task items render with correct status indicators
- Cost amounts are right-aligned and use tabular figures
- Hover state shows subtle background (neutral-700)
- GO TO STEP 3

---

### STEP 3: Implement tree item context menus
**Actor**: Frontend developer
**Action**:
Define VS Code tree item context menus via contribution points (package.json):

```json
"menus": {
  "view/item/context": [
    {
      "command": "bernstein.killAgent",
      "when": "view == bernstein.agents && viewItem == agent.active",
      "group": "1_agent_actions@1"
    },
    // ... more menu items per spec
  ]
}
```

Menu behavior:
- Right-click agent (active) → "Kill Agent", "Inspect", "Show Logs"
- Right-click task (open/claimed/in_progress) → "Prioritize", "Cancel", "Re-assign"
- Each menu item invokes a command with the item's context data

**Output on SUCCESS**:
- Menu items appear on right-click
- Commands receive correct agent/task ID
- Menu items are filtered by when conditions (only show for correct states)
- GO TO STEP 4

---

### STEP 4: Implement status bar indicator
**Actor**: Frontend developer
**Action**:
Create status bar item showing:
```
🎼 3 agents  ·  7/12 tasks  ·  $0.42
```

**Details**:
- Icon: Custom bernstein icon (🎼, or SVG icon)
- Text: "X agents · Y/Z tasks · $cost"
- Numbers use tabular figures
- Colors: text neutral-100, accent (if interactive)
- Click behavior: Open dashboard webview
- Updates every 2-5 seconds (debounced)
- Never shows loading states (always shows last known value)

**Output on SUCCESS**:
- Status bar item visible bottom-right of VS Code
- Text updates when agents/tasks change
- Click opens dashboard
- GO TO STEP 5

---

### STEP 5: Implement dashboard webview structure
**Actor**: Frontend developer
**Action**:
Create dashboard webview (new panel/tab) with this card-based layout:

```
┌─────────────────────────────────────────────────────────────┐
│ Bernstein Dashboard                                    [x]   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────┐ ┌────────┐│
│  │ 3           │  │ 7/12        │  │ 98%      │ │ $0.42  ││
│  │ Agents      │  │ Tasks Done  │  │ Success  │ │ Spent  ││
│  └─────────────┘  └─────────────┘  └──────────┘ └────────┘│
│                                                             │
│  Agent Cards:                                               │
│  ┌────────────────────┐  ┌────────────────────┐            │
│  │ backend-abc        │  │ qa-def             │            │
│  │ sonnet · 2m        │  │ sonnet · 1m        │            │
│  │ $0.12 · 4/8 tasks  │  │ $0.08 · 3/4 tasks  │            │
│  │ [████████░░] 100%  │  │ [████████░░] 75%   │            │
│  └────────────────────┘  └────────────────────┘            │
│                                                             │
│  Cost Timeline:                                             │
│  │   $0.5 ▁▂▃▂▁▂▃▄▅▄▃▄▅▆  (sparkline)                    │
│  │   $0.0 └─────────────────                              │
│  └─────────────────────────────────────────────────────────┘
│                                                             │
```

**Details**:
- 4 stat cards at top: Agents, Tasks Done (fraction), Success Rate (%), Cost
- Agent cards in grid: name, model, elapsed, cost, task count, progress bar
- Cost burn chart: sparkline (not full chart)
- All cards: neutral-900 background, neutral-800 border (optional), 16px padding
- Progress bars: indigo-600 for active portion
- No shadows, no rounded corners (max 4px border-radius)
- Skeleton loading state while fetching (light gray placeholder blocks, not "Loading...")

**Output on SUCCESS**:
- Dashboard webview loads and renders stat cards
- Agent cards display with correct data
- Progress bars show agent progress
- Cost sparkline renders
- GO TO STEP 6

---

### STEP 6: Implement real-time data updates in dashboard
**Actor**: Frontend developer
**Action**:
Connect dashboard to Bernstein API:
- Establish SSE connection to `/tasks` endpoint
- Listen for task updates, agent updates, cost updates
- Update state in webview without full page reload
- Debounce updates to max 2 updates/second

**Details**:
- On agent.started event → add card, show loading state
- On agent.completed event → mark card complete, show elapsed time
- On task.completed event → increment "Tasks Done" counter, update progress bar
- On cost event → update cost display and sparkline
- No polling, use server-sent events

**Output on SUCCESS**:
- Changes in Bernstein API reflected in dashboard within 500ms
- UI remains responsive during rapid updates
- Debounce prevents > 2 updates/second
- GO TO STEP 7

---

### STEP 7: Implement dashboard interactions
**Actor**: Frontend developer
**Action**:
Add click handlers:
- Click stat card → show details (agents list, completed tasks, cost breakdown)
- Click agent card → open agent output in editor
- Click progress bar → show task list for that agent
- Right-click agent card → same context menu as sidebar (kill, logs, inspect)

**Output on SUCCESS**:
- All interactive elements have hover states
- Click actions work (open output, show details)
- Keyboard navigation works (Tab between cards)
- GO TO STEP 8

---

### STEP 8: Implement performance optimizations
**Actor**: Frontend developer
**Action**:
Ensure extension meets performance budget:
- Extension startup: < 500ms
- Dashboard load: < 1s (from command invocation)
- Tree view refresh: < 100ms
- UI update (on API event): < 16ms (smooth 60fps)

**Details**:
- Lazy-load dashboard webview (don't create until user opens it)
- Memoize tree items (don't re-render unnecessarily)
- Use virtual scrolling for large agent/task lists (> 100 items)
- Debounce API polling to 2 updates/second max
- Cache API responses with 5-second TTL

**Measurements**:
- Use Chrome DevTools Performance tab to profile
- Trace tree view updates
- Measure webview initialization time
- Verify no memory leaks (extension memory < 50MB after 10 min)

**Output on SUCCESS**:
- Bundle size < 1MB (.vsix)
- Dashboard opens in < 1s
- Tree updates smooth (no janky scrolling)
- No memory leaks over 10 min
- GO TO STEP 9

---

### STEP 9: Theme compliance and accessibility
**Actor**: Frontend developer
**Action**:
Ensure UI respects VS Code theme and accessibility standards:

**Theme compliance**:
- Use VS Code CSS custom properties for colors (--vscode-*)
- Light theme: convert indigo to match light theme aesthetics
- Dark theme: use specified indigo palette
- High contrast: add border outlines (if --vscode-contrastBorder is set)

**Accessibility**:
- All text ≥ 13px (readable without zoom)
- Color contrast ≥ 4.5:1 (WCAG AA)
- Interactive elements ≥ 44px touch target
- Keyboard nav: Tab between items, Enter/Space to activate
- Screen reader: aria-label on icon buttons, role="treeitem" for tree items
- No purely color-based status (always use text or symbols)

**Output on SUCCESS**:
- Extension loads in light, dark, and high-contrast themes
- axe accessibility checker reports zero violations
- Keyboard nav works (Tab, Arrow keys)
- Screen reader announces tree items correctly
- GO TO STEP 10

---

### STEP 10: Polish animations and loading states
**Actor**: Frontend developer
**Action**:
Add subtle animations and loading indicators:

**Animations**:
- Tree item expand/collapse: instant (no animation)
- Status dot pulse (● to ◐ to ◑ to ●) when agent is running (200ms cycle)
- Dashboard card entrance: fade-in 200ms (not slide or scale)
- Progress bar fill: smooth transition (100ms)
- Status bar update: instant (no animation)

**Loading states**:
- Tree items: skeleton placeholder (gray block, 13px height) before data loads
- Dashboard: stat cards show placeholder (–, empty value) until populated
- Agent card: progress bar shows "0%" until first update
- Never show "Loading..." text (too noisy)

**Output on SUCCESS**:
- Animations are smooth (60fps) and subtle
- Loading states are visually clear but not distracting
- GO TO STEP 11

---

### STEP 11: Verify asset sizes and icons
**Actor**: Frontend developer / QA
**Action**:
Validate that all visual assets meet requirements:

**Icon requirements**:
- Sidebar icon: 128x128 PNG (bernstein-icon.png)
- Status bar icon: 24x24 (or use VS Code built-in)
- Dashboard icon: any size (renders at webview size)
- All icons: monochrome (no color), clean lines

**Asset checklist**:
- [ ] bernstein-icon.png is 128x128 or larger
- [ ] Icon is legible at 16x16 (status bar size)
- [ ] All screenshots in media/screenshots/ are 1280x720 or larger
- [ ] Dashboard can render with 0 agents/tasks (empty state)
- [ ] Dashboard can render with 50+ agents (performance tested)

**Output on SUCCESS**:
- All icons render clearly at all sizes
- Bundle size is < 1MB
- Dashboard renders correctly at all aspect ratios
- GO TO STEP 12

---

### STEP 12: Manual testing and polish
**Actor**: Frontend developer / QA
**Action**:
Test on real VS Code and Cursor installations:

**Test environment setup**:
- Install extension from built .vsix file
- Start Bernstein server on localhost:8052
- Create a test project with 3 agents and 5 tasks
- Run for 5 minutes, observe UI

**Test cases**:
- [ ] Sidebar loads immediately without lag
- [ ] Tree items expand/collapse responsively
- [ ] Right-click context menus appear correctly
- [ ] Status bar is visible and updates every 2-5s
- [ ] Dashboard opens in < 1s
- [ ] Dashboard shows all 4 stat cards
- [ ] Agent cards render with correct data
- [ ] Cost sparkline shows values over time
- [ ] Clicking an agent card opens its output
- [ ] No console errors in extension log
- [ ] No memory growth over 5 min (checked via VS Code Performance profiler)
- [ ] Works in light, dark, and high-contrast themes
- [ ] Works in both VS Code and Cursor

**Output on SUCCESS**:
- All tests pass
- No visual bugs or janky animations
- Polish is "Decision-Grade Quiet Command" level
- GO TO STEP 13

---

### STEP 13: Package and publish extension
**Actor**: GitHub Actions (via separate publish workflow)
**Action**:
Extension UX is complete, polished, and tested. Extension is packaged and published to marketplaces via WORKFLOW-extension-publish.

**Output**:
- Extension published to VS Code Marketplace with screenshots in README
- Extension published to Open VSX with Cursor verification
- SUCCESS

---

## Observable States (By User Perspective)

### Customer (VS Code User)

| State | What they see | When | How to exit |
|---|---|---|---|
| Not installed | No Bernstein icon in activity bar | Before install | Install from marketplace |
| Installed, server not running | Bernstein icon visible, but tree shows "Server not running" | After install, before launching Bernstein | Run `bernstein run` or set bernstein.apiUrl to correct URL |
| Server running, idle | Sidebar shows 0 agents, 0 tasks | Server running but no work | Create a task via CLI |
| Agent running | Sidebar shows 1+ agents with ● status dot, tree updates every 2-5s | Agent spawned | Agent completes or user kills it |
| Viewing dashboard | Dashboard panel open, 4 stat cards visible, agent cards updating | User opened dashboard | Close dashboard panel |
| Hovering tree item | Subtle background highlight (neutral-700) | Mouse over tree item | Move mouse away |
| Right-clicking agent | Context menu appears (Kill, Logs, Inspect) | Right-click active agent | Click menu item or press Escape |
| Extension error (no API) | Tree shows warning icon + "Cannot connect to Bernstein API" | Server unreachable | Fix API URL in settings |

### Operator (Developer monitoring the system)

| State | What they see | Where | How to interpret |
|---|---|---|---|
| Normal operation | Tree view updates every 2-5s with agent/task counts, status bar shows "3 agents · 7/12 tasks · $0.42" | Sidebar + status bar | System working as expected |
| Task completed | ✓ checkmark appears next to task, stat card increments "Tasks Done" | Tree view + dashboard | Task execution succeeded |
| Agent active | ● status dot, shows elapsed time and cost | Tree view | Agent is currently running |
| Agent idle | ○ hollow dot, shows total time and total cost | Tree view | Agent is not running but not cleaned up |
| High cost | Dashboard cost stat is highlighted or shows warning | Dashboard | May want to kill agents to save money |
| Connection lost | Tree shows "Reconnecting..." or red warning icon | Sidebar | API is unreachable, needs troubleshooting |

---

## Handoff Contracts

### Extension Host → Bernstein API (fetch agents)
**Endpoint**: `GET /tasks?status=open`
**Timeout**: 5s
**Success response**:
```json
{
  "tasks": [
    {
      "id": "abc123",
      "title": "Add JWT middleware",
      "status": "claimed",
      "assigned_agent": "backend-xyz",
      "created_at": "2026-03-29T12:00:00Z"
    }
  ]
}
```
**Failure**: Show "Cannot connect" in tree view, allow user to retry via `Bernstein: Refresh` command

---

### Extension Host → Bernstein API (subscribe to updates via SSE)
**Endpoint**: `GET /tasks/events` (server-sent events stream)
**Event format**:
```
event: task.started
data: {"id": "abc123", "status": "in_progress"}
```
**Timeout**: 30s (connection timeout) — reconnect with exponential backoff
**On failure**: Fall back to polling (GET /tasks every 5s)

---

### Extension Host → Bernstein API (execute command)
**Endpoint**: `POST /tasks/{id}/kill`
**Timeout**: 5s
**Success**: Return 204 No Content
**Failure**: Return 400 with error message, show in VS Code error message box

---

## Cleanup Inventory

Dashboard webview:
- Created: When user opens dashboard for first time
- Destroyed: When user closes dashboard panel
- Cleanup method: Automatic (VS Code disposes webview)

Tree view state:
- Created: When extension activates
- Destroyed: When extension deactivates (VS Code closes or extension disabled)
- Cleanup method: Automatic (VS Code manages lifecycle)

SSE connection:
- Created: When extension activates
- Destroyed: When extension deactivates
- Cleanup method: Graceful close (call `.close()` on EventSource)

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Tree view loads agents | Extension activates, API returns 3 agents | 3 agent items visible in tree view within 2s |
| TC-02: Agent status updates | API sends agent.completed event | Agent status dot changes from ● to ○ within 500ms |
| TC-03: Task marked complete | API sends task.completed event | Task row shows ✓ checkmark within 500ms |
| TC-04: Dashboard loads | User opens `Bernstein: Show Dashboard` | Dashboard renders in < 1s, 4 stat cards visible |
| TC-05: Cost updates | API sends cost update | Status bar cost value updates within 500ms |
| TC-06: Tree item right-click | User right-clicks agent | Context menu appears with 3 items (kill, logs, inspect) |
| TC-07: Kill agent command | User selects "Kill Agent" from menu | API POST /tasks/{id}/kill called, agent removed from tree within 1s |
| TC-08: Debounce test | API sends 10 updates in 1s | UI updates at most 2 times (debounce working) |
| TC-09: Light theme | VS Code theme set to light | UI colors adapt (indigo, text colors, backgrounds) |
| TC-10: High contrast mode | VS Code high contrast theme enabled | UI borders appear, contrast ≥ 4.5:1 |
| TC-11: Empty state | API returns 0 agents and 0 tasks | Tree shows "No agents running", dashboard shows 0 stat values |
| TC-12: Many agents (50+) | API returns 50 agents | Tree view remains responsive, virtual scrolling active |
| TC-13: Connection lost | API server goes down | Tree shows warning, "Reconnecting..." status, automatically retries |
| TC-14: Keyboard nav | User presses Tab + Arrow keys | Focus moves through tree items, Enter expands/collapses |
| TC-15: Screen reader | User enables screen reader | All tree items and buttons announced correctly |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Bernstein API is available on localhost:8052 by default | Verified: package.json has default config | If API is on different port, user must configure |
| A2 | VS Code provides SSE EventSource API | Verified: standard web API | If using older VS Code, SSE not available (use polling fallback) |
| A3 | CSS custom properties (--vscode-*) are available for theming | Verified: VS Code provides these in webview context | Themes may not work correctly in older VS Code versions |
| A4 | Tree view item icons come from VS Code icon library | Verified: esbuild resolves VS Code icons | Icons may not render if library missing |
| A5 | Dashboard webview can be opened as a side panel | Verified: ViewColumn.Beside is available in VS Code API | If older VS Code, panel may open in different location |
| A6 | Debouncing to 2 updates/second is sufficient for UX | Not verified; depends on network latency and agent activity | If too slow, may appear unresponsive; if too fast, may flicker |
| A7 | Status bar item can display custom text with icon | Verified: VS Code status bar API supports both | Status bar appearance may differ in Cursor |
| A8 | Users will have Bernstein server running on localhost | Not verified; assumption based on extension use case | If not running, extension shows graceful error |

---

## Open Questions

- Should the dashboard be a webview panel, or a custom editor (editable view)?
- Should agent cards show full logs in the dashboard, or only summary stats?
- Should cost be broken down by agent, task, and model, or just total?
- What precision for cost display (2 decimals, 3 decimals)?
- Should users be able to drag and drop tasks to prioritize them in the sidebar?
- Should the extension show a welcome page on first install?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial design: Created design system with color palette, typography, spacing | Documented in this spec |
| — | Tree view component structure already defined in package.json | Verified: contributions.views and contribution.viewContainers present |
| — | Dashboard is not yet implemented (webview) | Noted as Step 5 implementation task |
| — | Status bar text "🎼 3 agents · 7/12 tasks · $0.42" is proposed, not yet implemented | Noted as Step 4 implementation task |
| — | Context menus are defined in package.json but handlers not implemented | Noted as Step 3 implementation task |

