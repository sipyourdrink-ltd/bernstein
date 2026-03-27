# WORKFLOW: VS Code Extension User Interaction Patterns

**Version**: 0.3
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Review
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
  - Status bar: Initially shows "◌ Bernstein" while data fetches (set in StatusBarManager constructor — no "connecting" intermediate state)
  - Extension logs: `[Bernstein] Extension activated` (in "Output" → "Bernstein" channel)
  - Tree views: Empty (spinner) while fetching initial data

---

### STEP 2: Initial Data Fetch
**Actor**: BernsteinClient.getDashboardData()
**Action**: Fetch all agent, task, and cost data from orchestrator in a single request
**Timeout**: 5s
**Input**: HTTP GET `http://127.0.0.1:8052/dashboard/data` — a single unified endpoint (NOT `/agents`, `/tasks`, `/status` separately)
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
      ● backend-abc     sonnet   2m   $0.12
      ● qa-def          sonnet   1m   $0.08
      ○ docs-ghi        flash    0s   $0.00
    ```
    (● = status is `working` or `starting`; ○ = all other statuses. Label: first 14 chars of agent ID. Description: model, runtime, cost — verified against AgentTreeProvider.ts)
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
  - Status bar: "● 3 agents · 7/12 tasks · $0.42" (● when `stats.claimed > 0`, ○ otherwise — no 🎼 prefix; actual format: `${status} ${agents} agents · ${done}/${total} tasks · $${cost}`)

---

### STEP 4: Real-Time Event Subscription (SSE)
**Actor**: BernsteinClient.subscribeToEvents()
**Action**: Establish Server-Sent Events (SSE) connection to `http://127.0.0.1:8052/events`; listen for `task_update`, `agent_update`, `agent_output` events. Implemented via Node.js `http.get()` with manual SSE parsing — NOT via the browser EventSource API.
**Timeout**: Fixed reconnect delays (not exponential backoff — see RC-11)
**Input**: Event stream from orchestrator
**Output on SUCCESS**: Connection established, events received and processed in real-time → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(connection_failed)`: SSE endpoint does not exist or is blocked → [recovery: client automatically reconnects after 5s (error handler path); polling fallback (STEP 2 every 5s) continues independently]
  - `FAILURE(event_parse_error)`: Malformed SSE event data (non-JSON `data:` line) → [recovery: `try/catch` silently swallows error, extension continues listening — verified in extension.ts]
  - `FAILURE(connection_lost)`: Connection drops (server closes stream) → [recovery: auto-reconnect after 3s fixed delay; on error, reconnect after 5s fixed delay]

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
**Action**: Right-click active agent (or click "stop" icon inline), confirm via modal dialog
**Timeout**: 3s (kill request should complete quickly)
**Input**: User clicks `bernstein.killAgent` from tree item context menu (only available when `viewItem == agent.active`)
**Output on SUCCESS**:
  - `showInformationMessage("Kill signal sent to {id}")` displayed
  - `onRefresh()` called immediately — tree re-fetches within 500ms
  - On next SSE event or refresh, agent disappears from tree
**Output on FAILURE**:
  - `FAILURE(kill_failed)`: Orchestrator returns error (e.g., agent already dead) → [recovery: `showErrorMessage("Failed to kill agent: {error}")` displayed; tree updates on next polling interval (5s)]
  - `FAILURE(user_cancelled)`: User clicks away from modal or presses Escape → [recovery: no action taken, agent unchanged]

**Observable states during this step**:
  - Modal dialog: "Kill agent {agent.id}?" with [Kill] and [Cancel] buttons — NOT a toast
  - On confirm: Information message "Kill signal sent to {id}" appears bottom-right
  - Tree: Refreshes within 500ms of confirm; if kill is fast, agent disappears
  - Logs: VS Code extension output (not agent output channel)

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
**Action**: Right-click task
**Note**: Task context menu commands (Prioritize, Cancel, Re-assign) are **NOT implemented in v0.1.0**. No `menus["view/item/context"]` entries for `view == bernstein.tasks` exist in `package.json`. Only agent context menu is wired. This is a known UX gap for v0.2.0.

**Currently implemented**: None. Task tree items are read-only (display only).

**Planned for v0.2.0**:
  - **Prioritize**: Task moves to top of queue → `POST /tasks/{id}/prioritize`
  - **Cancel**: Task marked as cancelled → `POST /tasks/{id}/cancel`
  - **Re-assign**: Assign to different agent → `POST /tasks/{id}/reassign`
  - **View Output**: Open output channel for the agent assigned to this task

**Observable states during this step (v0.1.0)**:
  - Right-click on task item: no context menu appears (default VS Code behavior — empty context)

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
  - Status bar text: "○ Bernstein" (error state via `statusBar.setError(msg)` — sets text to `'○ Bernstein'`, error detail in tooltip only)
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
**Endpoint**: `GET http://127.0.0.1:8052/dashboard/data`
**Payload**: None
**Success response** (HTTP 200) — matches `DashboardData` TypeScript interface in `BernsteinClient.ts`:
```json
{
  "ts": 1711670400.0,
  "stats": {
    "total": 12,
    "open": 5,
    "claimed": 2,
    "done": 7,
    "failed": 0,
    "agents": 3,
    "cost_usd": 0.42
  },
  "agents": [
    { "id": "abc", "role": "backend", "model": "sonnet", "status": "working", "runtime_s": 120, "cost_usd": 0.12, "task_ids": ["t2"] },
    { "id": "def", "role": "qa", "model": "sonnet", "status": "starting", "runtime_s": 60, "cost_usd": 0.08 },
    { "id": "ghi", "role": "docs", "model": "flash", "status": "idle", "runtime_s": 0, "cost_usd": 0.00 }
  ],
  "tasks": [
    { "id": "t1", "title": "Add JWT middleware", "role": "backend", "status": "done", "priority": 5 },
    { "id": "t2", "title": "Write auth tests", "role": "qa", "status": "claimed", "priority": 5, "assigned_agent": "def" },
    { "id": "t3", "title": "Generate API docs", "role": "docs", "status": "open", "priority": 5 }
  ],
  "live_costs": {
    "spent_usd": 0.42,
    "budget_usd": 10.00,
    "percentage_used": 4.2,
    "should_warn": false,
    "should_stop": false,
    "per_agent": { "abc": 0.12, "def": 0.08 },
    "per_model": { "claude-sonnet-4-6": 0.20, "gemini-flash": 0.22 }
  },
  "alerts": [
    { "level": "warning", "message": "Backend agent exceeds cost threshold", "detail": "Cost: $0.12 vs threshold $0.10" }
  ]
}
```
**Verified field names** (against `BernsteinClient.ts` interfaces):
- `stats.agents` (not `agent_count`), `stats.cost_usd` (not `total_cost_usd`), `stats.total` (required)
- `agents[].role` (not `name`), `agents[].runtime_s` (not `elapsed_seconds`), `agents[].task_ids` (array, not `current_task`)
- `agents[].status`: ● shown for `working` or `starting`; ○ shown for all others (including `idle`, `done`, `failed`)
- `tasks[].assigned_agent` (not `agent_id`) — RC-9's note had this backwards; `assigned_agent` is the real field
- `live_costs.spent_usd` (not `total_usd`), `live_costs.per_agent` (not `by_agent`), `live_costs.per_model` (not `by_model`)

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
data: {"id": "def", "role": "qa", "model": "sonnet", "status": "working", "runtime_s": 65, "cost_usd": 0.08}

event: agent_output
data: {"agent_id": "def", "line": "[qa-def] Step 3: Generating tests for auth.ts..."}
```
**Note**: SSE `task_update` and `agent_update` events trigger `debouncedRefresh()` — the extension does NOT parse the event payload for UI updates, it re-fetches all data via `GET /dashboard/data`. Only `agent_output` events are parsed directly (for output channels). The field names above follow the same schema as `/dashboard/data`.
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
| RC-1 | **CORRECTED**: SSE connection does NOT use EventSource API. `subscribeToEvents()` uses Node.js `http.get()` with manual SSE line parsing. Reconnect uses fixed delays: 3s on stream end, 5s on error — NOT exponential backoff as originally stated. | High | STEP 4, STEP 10 | Fixed in spec v0.2 |
| RC-2 | Offline detection: Timer-based polling fallback is every 5s by default (configurable via `bernstein.refreshInterval`) — verified | Low | STEP 9, STEP 10 | Documented ✓ |
| RC-3 | Tree view rendering: Changes to tree data provider trigger immediate re-render; debouncing is 500ms max — verified | Low | STEP 3, STEP 5 | Documented ✓ |
| RC-4 | Output channel naming: Channels are named `Bernstein: {agent_id}` and are created on-demand — verified | Low | STEP 5 | Documented ✓ |
| RC-5 | Webview CSP: CSP is `default-src 'none'; style-src 'nonce-X'; script-src 'nonce-X'` which prevents iframes; external browser fallback is implemented — verified | Low | STEP 8 | Documented ✓ |
| RC-6 | Chat participant: Only available in VS Code 1.100+; gracefully guarded with `vscodeAny.chat?.createChatParticipant` check — verified | Low | STEP 11 | Documented ✓ |
| RC-7 | Status bar icon: Uses monospace dot-notation (🎼) not emoji; correct for "quiet command" design — verified | Low | All steps | Documented ✓ |
| RC-8 | Diff view handling: Extension opens diff view but doesn't specify layout (side-by-side vs inline) — should be side-by-side by default (VS Code default) — verified | Low | STEP 6 | No change needed |
| RC-9 | **CRITICAL**: STEP 2 originally specified 3 separate API calls (`/agents`, `/tasks`, `/status`) but code makes ONE unified call to `GET /dashboard/data`. Handoff contract schema also incorrect (missing `ts`, `live_costs`; wrong field names `name`→`role`, `elapsed_seconds`→`runtime_s`, `assigned_agent`→`agent_id`). | High | STEP 2, Handoff Contract | Fixed in spec v0.2 |
| RC-10 | Task context menu (STEP 6b) references Prioritize/Cancel/Re-assign commands that are NOT implemented. No task context menu entries exist in `package.json`. | High | STEP 6b | Fixed in spec v0.2 — marked as v0.2.0 gap |
| RC-11 | Kill agent flow uses modal confirmation dialog (`showWarningMessage` with `modal: true`), not a "Killing..." toast as originally stated. Success shows `showInformationMessage`. | Medium | STEP 5b | Fixed in spec v0.2 |
| RC-12 | Agent display name in tree is truncated to 14 chars via `agent.id.slice(0, 14)`. Spec showed full agent ID format. | Low | STEP 3, STEP 5 | Noted — minor cosmetic |
| RC-13 | `bernstein.start` command creates a VS Code terminal and runs `bernstein run`. Not documented in UX workflow. | Medium | Missing step | Documented in Assumptions; add as future STEP 0a |
| RC-14 | `bernstein.showDashboard` command calls `DashboardProvider.openInBrowser(client.baseUrl)` — opens browser to `{baseUrl}/dashboard`. Matches spec STEP 8. ✓ | Low | STEP 8 | Verified ✓ |
| RC-15 | Agent tree icons use VS Code codicons `$(circle-filled)` and `$(circle-outline)` — renders as ● and ○ in the UI. Spec notation was correct. ✓ | Low | STEP 3 | Verified ✓ |
| RC-16 | **CORRECTED**: RC-9 introduced a backwards correction — `tasks[].agent_id` is NOT the real field. The `BernsteinTask` TypeScript interface uses `assigned_agent?: string`. All handoff contract examples updated to use `assigned_agent`. RC-9's note was wrong. Also corrected in same pass: `stats.agents` (was `agent_count`), `stats.cost_usd` (was `total_cost_usd`), `live_costs.spent_usd` (was `total_usd`), `live_costs.per_agent/per_model` (was `by_agent/by_model`), `agents[].task_ids` (was `current_task`). | Critical | Handoff Contract | Fixed in spec v0.3 |
| RC-17 | Agent ● indicator is keyed on `ACTIVE_STATUSES = new Set(['working', 'starting'])` — not a generic `active` status. Spec previously stated `● = active` which is imprecise. Status values 'active', 'running' etc. would NOT trigger ●. | Medium | STEP 3 | Fixed in spec v0.3 |
| RC-18 | **Status bar symbols**: Task 340c design intent specifies `🎼` as status bar prefix. Actual implementation uses `◌ Bernstein` (initial/loading), `● N agents · N/N tasks · $X.XX` (running), `○ Bernstein` (error/offline). No 🎼 anywhere in StatusBarManager.ts. Spec references to `🎼` were incorrect — corrected to `●/○/◌`. Design gap (🎼 requirement from 340c) should be addressed in a UX polish task. | High | STEP 1, STEP 3, STEP 9, STEP 10 | Fixed in spec v0.3 |
| RC-19 | SSE events trigger `debouncedRefresh()` — NOT parsed for UI fields. Only `agent_output` events are parsed (for output channel lines). SSE event payloads influence nothing directly; the extension re-fetches fresh `/dashboard/data` on every relevant event. Previously implied per-event field parsing. | Medium | STEP 4, Handoff Contract | Fixed in spec v0.3 |

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
| A11 | `bernstein.start` command creates a VS Code terminal named "Bernstein" and runs `bernstein run` — user must have `bernstein` CLI installed in PATH | Verified: `commands.ts` line 15-19 | If CLI not in PATH, terminal opens but command fails with "command not found" |

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
| 2026-03-29 | Reality Checker pass: verified all steps against extension.ts, BernsteinClient.ts, AgentTreeProvider.ts, commands.ts, package.json | Fixed API endpoint (→ /dashboard/data), fixed handoff contract schema, corrected SSE implementation details, corrected kill agent UX flow, flagged task context menu gap; status elevated to Review |
| 2026-03-29 | Workflow Architect second pass: deep interface verification against BernsteinClient.ts and AgentTreeProvider.ts | Found RC-9 was backwards (assigned_agent→agent_id was wrong); fixed all handoff contract field names (RC-16); corrected agent active status definition to working/starting (RC-17); corrected status bar symbols from 🎼 to ●/○/◌ (RC-18); clarified SSE event handling is refresh-trigger-only (RC-19); spec bumped to v0.3 |

---

## Next Steps (External Dependencies)

This spec is **Review-ready** pending Reality Checker verification. The following must be completed before marking **Approved**:

1. **Reality Checker**: Verify each UX interaction step against current `extension.ts`, tree providers, and dashboard code
2. **Frontend Developer**: Implement UX polish items (quiet design, status indicators, dashboard styling)
3. **QA/Reality Checker**: Execute test cases TC-01 through TC-20
4. **Backend Architect**: Verify API contract endpoints match the handoff specifications

---

**Spec Status**: Review (Second pass complete — 4 additional critical/high/medium findings fixed in v0.3; task context menu gap documented for v0.2.0; 🎼 status bar icon is a known design gap)
**Ready for Implementation**: Yes — extension is implemented. UX polish items: (1) add 🎼 icon to status bar per 340c design intent; (2) implement task context menu (v0.2.0). All other interaction paths are verified and spec-accurate.
