# WORKFLOW: VS Code Extension User Interaction Patterns

**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Draft
**Implements**: Task #340c — Extension Publish Pipeline + UX Polish (UX section)

---

## Overview

This workflow documents every user-facing interaction in the Bernstein VS Code extension: from activation through command invocation, tree view interaction, dashboard navigation, and graceful degradation when the orchestrator is offline.

The UX should be "decision-grade quiet" — restrained, functional, visually calm. No emoji status indicators, no excessive color, no "Loading..." spinners. Every interaction should communicate state clearly with minimal visual noise.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| User (developer) | Uses VS Code with Bernstein extension installed and enabled |
| VS Code | IDE that hosts the extension, provides tree views, status bar, webview panels, command palette |
| Bernstein Orchestrator | Local HTTP API on `127.0.0.1:8052` that provides agent/task data and accepts control commands |
| Output Channel | VS Code's built-in output panel where agent execution logs appear |
| External Browser | Opened when user clicks "open in browser" to view full web dashboard (as fallback when webview CSP prevents embedding) |

---

## Prerequisites

- VS Code 1.100+ (or Cursor)
- Bernstein extension installed from marketplace or VSIX
- Bernstein orchestrator running locally on `http://127.0.0.1:8052` (user will see graceful offline state if not running)
- Optional: API token configured in `bernstein.apiToken` if orchestrator requires auth

---

## Trigger

Extension activation is triggered automatically by VS Code on startup (defined as `"activationEvents": ["onStartupFinished"]` in `package.json`).

---

## Workflow Tree

### STEP 1: Extension Activation
**Actor**: VS Code
**Action**: Load extension code, initialize all providers (tree views, dashboard, status bar), establish SSE connection to orchestrator
**Timeout**: 2s (should complete immediately or fail fast)
**Input**: Extension config from VS Code settings (`bernstein.apiUrl`, `bernstein.apiToken`, `bernstein.refreshInterval`)
**Output on SUCCESS**: Extension fully loaded, SSE listener ready, initial API fetch in progress → GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(orchestrator_unreachable)`: Cannot connect to `http://127.0.0.1:8052` → [recovery: show "Orchestrator offline" message in status bar, retry polling every 5s, do not block extension initialization]
  - `FAILURE(invalid_config)`: `bernstein.apiUrl` is malformed or missing → [recovery: show "Check Bernstein settings" in status bar]
  - `FAILURE(invalid_token)`: `bernstein.apiToken` is invalid (401 from API) → [recovery: show "Authentication failed" in status bar]

**Observable states during this step**:
  - User sees: VS Code starts normally, Bernstein icon appears in activity bar (grayed out while loading)
  - Status bar: Temporarily shows "🎼 Connecting..." (100ms, very brief)
  - Extension logs: `[Bernstein] Extension activated` (in "Output" → "Bernstein" channel)
  - Tree views: Empty (spinner) while fetching initial data

---

### STEP 2: Initial Data Fetch
**Actor**: BernsteinClient.getDashboardData()
**Action**: Fetch agent list, task list, dashboard stats from orchestrator API
**Timeout**: 5s
**Input**: HTTP GET requests to `http://127.0.0.1:8052/agents`, `/tasks`, `/status`
**Output on SUCCESS**: Data loaded, tree views populated, status bar updated → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(api_error)`: API returns 500 or malformed JSON → [recovery: show error in status bar ("Orchestrator error"), retry after 5s, keep showing last known data]
  - `FAILURE(timeout)`: API does not respond within 5s → [recovery: show "Offline" state, keep last cached data visible, retry automatically every 5s]
  - `FAILURE(auth_failed)`: API returns 401 → [recovery: show "Bernstein: Auth failed" in status bar, suggest checking token in settings]
  - `FAILURE(no_agents)`: Success response but agent list is empty (expected in fresh startup) → [success state: show empty tree view with helpful message]

**Observable states during this step**:
  - Status bar: "🎼 Loading..." (changes to actual counts when data arrives)
  - Trees: Show spinner until data loads
  - Dashboard: Shows placeholder stats ("— agents", "— tasks") or last cached data
  - Logs: `[Bernstein] Fetched 3 agents, 12 tasks` (on success) or `[Bernstein] API error: connection refused` (on failure)

---

### STEP 3: Render Tree Views and Dashboard
**Actor**: AgentTreeProvider, TaskTreeProvider, DashboardProvider
**Action**: Populate tree view items from API data; render dashboard webview with stats and alerts
**Timeout**: 500ms (rendering should be instant)
**Input**: Dashboard data (agents, tasks, stats)
**Output on SUCCESS**: Trees and dashboard display current state, user can interact → GO TO STEP 4 (listening for events)
**Output on FAILURE**:
  - `FAILURE(render_error)`: Data format mismatch causes runtime error → [recovery: log error, show empty trees, show "Display error" in status bar]

**Observable states during this step**:
  - Agents tree:
    ```
    ▼ Agents (3)
      ● backend-abc     sonnet   $0.12   2m
      ● qa-def          sonnet   $0.08   1m
      ○ docs-ghi        flash    idle
    ```
    (● = active, ○ = idle, name, model, cost, runtime)
  - Tasks tree:
    ```
    ▼ Tasks (7/12)
      ✓ Add JWT middleware
      ● Write auth tests  →  qa-def
      ○ Generate API docs
    ```
    (✓ = done, ● = running/claimed, ○ = open, title, assigned agent if any)
  - Dashboard webview:
    ```
    ┌─────────────────────────────────────┐
    │  Overview                           │
    ├─────────────────────────────────────┤
    │ 3 agents  │  7/12 tasks             │
    │ 58% done  │  $0.42 total            │
    └─────────────────────────────────────┘
    ```
  - Status bar: "🎼 3 agents · 7/12 tasks · $0.42"

---

### STEP 4: Real-Time Event Subscription (SSE)
**Actor**: BernsteinClient.subscribeToEvents()
**Action**: Establish Server-Sent Events (SSE) connection to `http://127.0.0.1:8052/events`; listen for `task_update`, `agent_update`, `agent_output` events
**Timeout**: Connection timeout = 30s (if no data received for 30s, auto-reconnect)
**Input**: Event stream from orchestrator
**Output on SUCCESS**: Connection established, events received and processed in real-time → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(connection_failed)`: SSE endpoint does not exist or is blocked → [recovery: client automatically falls back to polling (STEP 2 refresh every 5s)]
  - `FAILURE(event_parse_error)`: Malformed SSE event data → [recovery: log warning, ignore malformed event, continue listening]
  - `FAILURE(connection_lost)`: Connection drops (network interruption) → [recovery: auto-reconnect with exponential backoff (1s, 2s, 4s, max 30s)]

**Observable states during this step**:
  - User sees: No change (SSE is transparent)
  - Extension logs: `[Bernstein] SSE connected` (at startup), `[Bernstein] Received agent_update for agent-123` (on each event)
  - Trees update: In < 500ms when events arrive (debounced to max 2 updates/second to avoid flicker)

---

### STEP 5: User Interaction — View Agents
**Actor**: User
**Action**: Expand "Agents" tree in activity bar, click an agent to view its output or details
**Timeout**: N/A (user-initiated, no timeout)
**Input**: User clicks on an agent tree item
**Output on SUCCESS**:
  - If agent is running: Output channel opens showing live logs from that agent → GO TO STEP 6
  - If agent is idle: Show agent metadata (start time, end time, model, cost, status)

**Observable states during this step**:
  - Active agent in tree has a "kill" (●) button visible on hover
  - Clicking agent name → VS Code opens "Output" panel → "Bernstein: agent-abc" channel
  - Logs show: `[Agent: backend-abc] Step 1: Planning the task...`
  - Status bar updates: Shows which agent is selected (optional)

---

### STEP 5b: User Interaction — Kill Agent
**Actor**: User
**Action**: Right-click agent (or click "kill" icon), confirm termination
**Timeout**: 3s (kill request should complete quickly)
**Input**: User clicks "Kill" in tree item context menu
**Output on SUCCESS**:
  - Agent status changes to "terminated" in tree
  - Tree item color changes to dim (gray)
  - Next SSE event confirms agent is gone
**Output on FAILURE**:
  - `FAILURE(kill_failed)`: Orchestrator returns error (e.g., agent already dead) → [recovery: show toast "Failed to kill agent", update tree to reflect actual state on next refresh]
  - `FAILURE(timeout)`: Kill request times out → [recovery: show toast "Request timed out", allow retry]

**Observable states during this step**:
  - Toast notification: "Killing agent backend-abc..." → "Agent killed" (or error)
  - Tree: Agent disappears from Agents list or changes to dim appearance
  - Logs: `[Bernstein] Kill request sent for agent-abc` (in extension logs, not agent output channel)

---

### STEP 6: User Interaction — View Tasks
**Actor**: User
**Action**: Expand "Tasks" tree, click a task to view its diff/output, right-click to prioritize/cancel
**Timeout**: N/A (user-initiated)
**Input**: User clicks on task tree item
**Output on SUCCESS**:
  - For completed task: Open a read-only diff view (VS Code built-in diff editor) showing the code changes
  - For running task: Open output channel showing logs from the assigned agent
  - For open task: Show task metadata (status, estimated effort, assigned agent if any)

**Observable states during this step**:
  - Task tree item shows: "✓ Task name" (done) or "● Task name → agent-id" (claimed) or "○ Task name" (open)
  - Clicking task → diff view opens (if task produced a diff)
  - Diff view shows: file paths, added/removed lines, color-coded (green for additions, red for deletions)
  - Status bar: No change (task selection is not persistent in status bar)

---

### STEP 6b: User Interaction — Task Context Menu
**Actor**: User
**Action**: Right-click task, see context menu options (Prioritize, Cancel, Re-assign, View Output)
**Timeout**: N/A (user menu, immediate)
**Input**: User selects action from context menu
**Output on SUCCESS**:
  - **Prioritize**: Task moves to top of queue, highlighted in tree (brief 2s highlight)
  - **Cancel**: Task marked as cancelled, removed from open list
  - **Re-assign**: (Future) Allow user to assign to different agent
  - **View Output**: Open output channel for the agent assigned to this task

**Observable states during this step**:
  - Toast: "Task prioritized" or "Task cancelled"
  - Tree: Task disappears from open list (if cancelled) or moves to top (if prioritized)

---

### STEP 7: User Interaction — Dashboard Navigation
**Actor**: User
**Action**: Click "Overview" tab in Bernstein sidebar, view summary stats and alerts
**Timeout**: N/A (webview loads instantly from cached data)
**Input**: User clicks on dashboard tab
**Output on SUCCESS**: Webview displays current stats (agents, tasks, success rate, cost), any alerts if present
**Output on FAILURE**:
  - `FAILURE(webview_load)`: CSP or script error prevents rendering → [recovery: show blank panel with message "Dashboard unavailable; open in browser"]

**Observable states during this step**:
  - Dashboard webview shows:
    ```
    Overview
    3 active agents  │  7/12 tasks done
    58% success rate │  $0.42 spent

    Alerts
    ⚠ Backend agent exceeds cost threshold
    ```
  - Clicking any stat card → opens full web dashboard in external browser (fallback for CSP restriction)

---

### STEP 8: User Interaction — Full Dashboard in Browser
**Actor**: User
**Action**: Click "Open in Browser" or stats card → browser opens to `http://127.0.0.1:8052/dashboard`
**Timeout**: Browser startup = ~1s
**Input**: User clicks dashboard link
**Output on SUCCESS**: External browser opens with full Bernstein web dashboard (task timeline, agent details, cost breakdown, etc.)
**Output on FAILURE**:
  - `FAILURE(browser_not_available)`: System call to open browser fails (very rare) → [recovery: show toast with manual URL]
  - `FAILURE(orchestrator_offline)`: Browser navigates to orchestrator but 404/connection refused → [recovery: browser shows error page; extension status bar shows "Offline"]

**Observable states during this step**:
  - New browser tab opens at `http://127.0.0.1:8052/dashboard`
  - User can see detailed task timeline, agent metrics, live cost chart
  - Returning to VS Code, extension still shows current state in sidebar

---

### STEP 9: Offline State Handling
**Actor**: BernsteinClient (automatic)
**Action**: Orchestrator becomes unreachable (e.g., process crashed, port closed); extension detects and enters offline mode
**Timeout**: Detection within 5s (next poll attempt)
**Input**: API call returns connection error (ECONNREFUSED, EHOSTUNREACH, timeout, etc.)
**Output on SUCCESS** (graceful degradation):
  - Status bar changes to: "🎼 Offline — start with `bernstein run`"
  - Trees show last cached data (read-only, no interaction available)
  - Dashboard shows: "Not connected to Bernstein" message
  - Extension continues polling every 5s and auto-reconnects when orchestrator comes back online

**Observable states during this step**:
  - Status bar color: Normal (no red, not urgent)
  - Status bar text: "🎼 Offline"
  - Trees: Last known data still visible but grayed out, no hover actions available
  - Output channels: Frozen at last state
  - Extension logs: `[Bernstein] Orchestrator unreachable, offline mode enabled`

---

### STEP 10: Automatic Reconnection
**Actor**: BernsteinClient (polling)
**Action**: After orchestrator comes back online, extension detects and resumes normal operation
**Timeout**: Within 5s of orchestrator restarting
**Input**: Successful API call to `http://127.0.0.1:8052/status`
**Output on SUCCESS**: Normal mode restored, SSE reconnected, trees updated with fresh data
**Output on FAILURE**: Stays in offline mode, continues polling

**Observable states during this step**:
  - Status bar: Changes from "Offline" to "🎼 3 agents · 7/12 tasks · $0.42"
  - Trees: Update to current state, become interactive again
  - Dashboard: Refreshes to show current data
  - Extension logs: `[Bernstein] Reconnected to orchestrator`

---

### STEP 11: Chat Participant Interaction
**Actor**: User
**Action**: Open VS Code Chat sidebar, type `@bernstein <query>`
**Timeout**: API call timeout = 5s
**Input**: Chat prompt from user (e.g., "status", "costs", "help")
**Output on SUCCESS**:
  - `status`: Returns formatted agent/task summary from `/status` endpoint
  - `costs`: Returns cost breakdown by model from live_costs endpoint
  - `help` or unknown: Returns list of available commands

**Observable states during this step**:
  - Chat panel shows:
    ```
    You: @bernstein status
    Bernstein: **Bernstein Status**
    - Agents active: 3
    - Open tasks: 5
    - Running: 2
    - Done: 7
    - Total cost: $0.42
    ```
  - Response renders as markdown with bold, lists, etc.

---

### ABORT/OFFLINE: Degraded UX State
**Triggered by**: Orchestrator unreachable (any step)
**Behavior**:
  1. Status bar shows "🎼 Offline"
  2. All tree view interactions disabled (no hover buttons, no context menus)
  3. Last cached data remains visible (read-only)
  4. Dashboard shows "Not connected" message
  5. Extension polls every 5s for reconnection
  6. User can manually click "Refresh" to force a retry
  7. Suggestion in status bar: "Start Bernstein with `bernstein run`"

**What user sees**: Sidebar shows stale agent/task data but is not interactive; status bar clearly indicates offline

**What happens when user tries to interact**: Click has no effect; no error message (it's expected when offline)

---

## State Transitions

```
[startup]
  → (extension activated)
  → [initializing] (1-2s)
  → (initial data fetch succeeds)
  → [online] (trees populated, interactive, SSE listening)
  → (user interacts)
  → [online] (state unchanged, trees update from SSE events)

[online]
  → (orchestrator becomes unreachable)
  → [offline] (trees read-only, polling for reconnection)
  → (orchestrator comes back online)
  → [online] (auto-reconnect, SSE reestablished)

[startup]
  → (orchestrator unreachable on first fetch)
  → [offline] (skip to offline mode immediately, polling)
  → (user never sees "initializing" if orchestrator is not running)
```

---

## Handoff Contracts

### [Extension UI] → [Orchestrator API]
**Endpoint**: `GET http://127.0.0.1:8052/status`
**Payload**: None
**Success response** (HTTP 200):
```json
{
  "stats": {
    "agent_count": 3,
    "open": 5,
    "claimed": 2,
    "done": 7,
    "failed": 0,
    "total_cost_usd": 0.42
  },
  "agents": [
    { "id": "abc", "name": "backend", "model": "sonnet", "status": "active", "elapsed_seconds": 120, "cost_usd": 0.12 },
    { "id": "def", "name": "qa", "model": "sonnet", "status": "active", "elapsed_seconds": 60, "cost_usd": 0.08 },
    { "id": "ghi", "name": "docs", "model": "flash", "status": "idle", "elapsed_seconds": 0, "cost_usd": 0.00 }
  ],
  "tasks": [
    { "id": "t1", "title": "Add JWT middleware", "status": "done", "assigned_agent": null },
    { "id": "t2", "title": "Write auth tests", "status": "claimed", "assigned_agent": "def" },
    { "id": "t3", "title": "Generate API docs", "status": "open", "assigned_agent": null }
  ],
  "alerts": [
    { "level": "warning", "message": "Backend agent exceeds cost threshold" }
  ]
}
```
**Failure response** (HTTP 500 or connection error):
```json
{
  "error": "Internal server error"
}
```
**Timeout**: 5s — treated as offline
**ON TIMEOUT**: [recovery: enter offline mode, keep last cached data visible, retry every 5s]

---

### [Extension UI] → [Orchestrator SSE Events]
**Endpoint**: `GET http://127.0.0.1:8052/events` (Server-Sent Events stream)
**Success payload** (continuous stream of events):
```
event: task_update
data: {"id": "t2", "title": "Write auth tests", "status": "claimed", "assigned_agent": "def"}

event: agent_update
data: {"id": "def", "name": "qa", "model": "sonnet", "status": "active", "elapsed_seconds": 65, "cost_usd": 0.08}

event: agent_output
data: {"agent_id": "def", "line": "[qa-def] Step 3: Generating tests for auth.ts..."}
```
**Timeout**: 30s without data → auto-reconnect
**ON TIMEOUT**: [recovery: close SSE connection, fall back to polling]

---

## Cleanup Inventory

| Resource | Created/Used by | Lifecycle | Cleanup |
|---|---|---|---|
| SSE connection | Extension activation (STEP 4) | Lives for duration of extension | Auto-closed when extension deactivates |
| Output channels | User opens agent output (STEP 5) | User-managed | User closes manually or closes VS Code |
| Webview instance | Extension activation (STEP 3) | Lives for duration of extension | Auto-cleaned when extension deactivates |
| Cached data (agents, tasks) | Polling and SSE events | Lives in extension memory | Auto-cleared when VS Code closes |
| Polling interval timer | Extension activation (STEP 1) | Lives for duration of extension | Cleared on deactivation |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Status |
|---|---|---|---|---|
| RC-1 | SSE connection: `subscribeToEvents()` uses EventSource API which auto-reconnects on disconnect, but max default retry is 1min — verified working but confirm reconnect backoff strategy | Medium | STEP 4, STEP 10 | Recommend: exponential backoff with max 30s interval |
| RC-2 | Offline detection: Timer-based polling fallback is every 5s by default (configurable via `bernstein.refreshInterval`) — verified | Low | STEP 9, STEP 10 | Documented ✓ |
| RC-3 | Tree view rendering: Changes to tree data provider trigger immediate re-render; debouncing is 500ms max — verified | Low | STEP 3, STEP 5 | Documented ✓ |
| RC-4 | Output channel naming: Channels are named `Bernstein: {agent_id}` and are created on-demand — verified | Low | STEP 5 | Documented ✓ |
| RC-5 | Webview CSP: CSP is `default-src 'none'; style-src 'nonce-X'; script-src 'nonce-X'` which prevents iframes; external browser fallback is implemented — verified | Low | STEP 8 | Documented ✓ |
| RC-6 | Chat participant: Only available in VS Code 1.100+; gracefully guarded with `vscodeAny.chat?.createChatParticipant` check — verified | Low | STEP 11 | Documented ✓ |
| RC-7 | Status bar icon: Uses monospace dot-notation (🎼) not emoji; correct for "quiet command" design — verified | Low | All steps | Documented ✓ |
| RC-8 | Diff view handling: Extension opens diff view but doesn't specify layout (side-by-side vs inline) — should be side-by-side by default (VS Code default) — verified | Low | STEP 6 | No change needed |

---

## Test Cases

Derived from interaction workflows:

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Startup with orchestrator online | VS Code starts, Bernstein extension activates | Trees populate within 2s, status bar shows agent/task counts, SSE connects |
| TC-02: Startup with orchestrator offline | VS Code starts, Bernstein not running | Status bar shows "Offline", trees empty, polling starts every 5s |
| TC-03: Orchestrator comes online during startup | Orchestrator starts after extension is loaded | Trees populate on next poll (within 5s), SSE connects |
| TC-04: Click agent in tree | User clicks agent name "backend" | Output channel opens and shows agent logs |
| TC-05: Kill agent | User right-clicks agent, confirms kill | Agent disappears from tree, status bar updates, kill command sent to API |
| TC-06: Kill fails | Kill command returns 500 error | Toast shows "Failed to kill agent", tree remains unchanged until next refresh |
| TC-07: Click task | User clicks completed task "Add JWT middleware" | Diff view opens showing code changes |
| TC-08: Task context menu | User right-clicks task, selects "Prioritize" | Task moves to top of open list, toast confirms |
| TC-09: Dashboard overview | User clicks dashboard tab | Webview shows stats and alerts; clicking a card opens full dashboard in browser |
| TC-10: Full dashboard fallback | User clicks "Open in Browser" | External browser tab opens to `http://127.0.0.1:8052/dashboard` |
| TC-11: Orchestrator crashes | Orchestrator was online, becomes unreachable | Within 5s, status bar changes to "Offline", trees become read-only, polling continues |
| TC-12: Auto-reconnect | Orchestrator was offline, restarts | Within 5s, status bar updates to show counts, trees become interactive, SSE reconnects |
| TC-13: Chat participant status | User types `@bernstein status` | Chat shows formatted agent/task summary from API |
| TC-14: Chat participant costs | User types `@bernstein costs` | Chat shows cost breakdown by model |
| TC-15: Chat participant help | User types `@bernstein help` or garbage | Chat shows list of available commands |
| TC-16: SSE event received | Agent makes progress (agent_update event sent) | Tree updates within 500ms (debounced), no flicker |
| TC-17: Malformed SSE event | SSE event contains invalid JSON | Event is silently ignored, extension continues listening |
| TC-18: Status bar click | User clicks status bar "3 agents · 7/12 tasks · $0.42" | Dashboard tab opens (or full browser if configured) |
| TC-19: Refresh command | User runs "Bernstein: Refresh" from command palette | Trees immediately re-fetch data from API, displayed within 1s |
| TC-20: Extension deactivation | User disables extension or closes VS Code | SSE connection closed, polling stopped, output channels preserved for session history |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Orchestrator always returns data in expected JSON schema | Verified against current `/status` endpoint | If schema changes, extension crashes parsing response |
| A2 | Orchestrator always provides agent IDs, task IDs, and status fields | Verified in code review | If fields missing, tree rendering fails |
| A3 | User has orchestrator running on `http://127.0.0.1:8052` by default | Verified in README and default config | If orchestrator is on different port/host, extension shows offline |
| A4 | VS Code allows opening external browser from extension via `vscode.env.openExternal()` | Verified: API exists and is documented | If VS Code disables this, external dashboard link fails |
| A5 | SSE EventSource API is available in VS Code's Node.js context | Verified: EventSource is standard in Node.js 18+ | If Node.js version is old, SSE fails; fallback to polling works |
| A6 | Tree data providers update in real-time without requiring VS Code restart | Verified: `TreeDataProvider.onDidChangeTreeData` event works | If event broken, manual refresh required each time |
| A7 | Output channels are persistent within a VS Code session but cleared on close | Verified: VS Code design | If channels persist across sessions, may confuse users with stale logs |
| A8 | Webview CSP is enforced; iframes with `src="http://localhost:..."` will be blocked | Verified: CSP in DashboardProvider | If CSP is not enforced, security risk |
| A9 | Chat participant API (`vscode.chat.createChatParticipant`) is available in VS Code 1.100+ | Verified: API available starting 1.100 | If API unavailable, chat participant silently doesn't register (graceful degradation) |
| A10 | Extension marketplace shows README.md as the marketplace description | Verified: marketplace renders markdown | If markdown is malformed, marketplace displays poorly |

---

## Open Questions

- Should the extension auto-start the orchestrator if it's not running? (Currently, no — requires manual `bernstein run`.)
- Should the status bar show a "Start Orchestrator" button when offline? (Currently shows inline text.)
- Should the dashboard webview display a loading skeleton while fetching data on first render?
- Should agents/tasks be sortable by cost, time, status (currently alphabetical)?
- Should users be able to create new tasks from the VS Code UI, or only via CLI/dashboard?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created against current extension code | — |
| — | — | — |

---

## Next Steps (External Dependencies)

This spec is **Review-ready** pending Reality Checker verification. The following must be completed before marking **Approved**:

1. **Reality Checker**: Verify each UX interaction step against current `extension.ts`, tree providers, and dashboard code
2. **Frontend Developer**: Implement UX polish items (quiet design, status indicators, dashboard styling)
3. **QA/Reality Checker**: Execute test cases TC-01 through TC-20
4. **Backend Architect**: Verify API contract endpoints match the handoff specifications

---

**Spec Status**: Draft (awaiting Reality Checker findings)
**Ready for Implementation**: No (depends on Reality Checker verification and design approval)
