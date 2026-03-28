# 340b — VS Code / Cursor Extension

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** large
**Depends on:** none

## Problem

Bernstein is CLI-only. 73% of developers use VS Code/Cursor. Without an IDE extension, users must switch between editor and terminal to monitor agents, view tasks, track costs. Every competitor (Cline, Roo Code, Continue.dev) has an extension. This is the #1 distribution channel for developer tools.

## Design

### Architecture

TypeScript extension with three tiers:
1. **Extension Host** (Node.js) — connects to Bernstein's HTTP API at localhost:8052 via SSE
2. **Tree Views** — native VS Code trees for agents and tasks
3. **Webview Sidebar** — React app for dashboard, cost charts, agent output

```
bernstein-vscode/
├── package.json           # Extension manifest
├── src/
│   ├── extension.ts       # activate/deactivate
│   ├── BernsteinClient.ts # HTTP + SSE client to localhost:8052
│   ├── TaskTreeProvider.ts    # TreeDataProvider for task list
│   ├── AgentTreeProvider.ts   # TreeDataProvider for agent list
│   ├── StatusBarManager.ts    # Live cost: "$(dollar) $4.23"
│   ├── DashboardProvider.ts   # WebviewViewProvider for sidebar
│   ├── OutputManager.ts       # Per-agent output channels
│   └── commands.ts            # Command palette commands
├── webview-ui/            # React app (Vite)
│   └── src/
│       ├── App.tsx
│       ├── Dashboard.tsx
│       ├── TaskList.tsx
│       └── CostChart.tsx
├── media/
│   └── bernstein-icon.svg
└── skills/
    └── bernstein-status/SKILL.md
```

### Features

**Activity Bar icon** — Bernstein logo in sidebar

**Agent tree view:**
```
▼ Agents (3 active)
  ● backend-abc123  sonnet  src/auth.py  $0.12
  ● qa-def456       sonnet  tests/       $0.08
  ○ docs-ghi789     flash   idle         $0.02
```

**Task tree view:**
```
▼ Tasks (5/12 done)
  ✓ Add JWT middleware
  ● Write auth tests (qa-def456)
  ○ Generate API docs
  ○ Add rate limiting
```

**Status bar:** `🎼 Bernstein: 3 agents | 5/12 tasks | $0.42`

**Command palette:**
- `Bernstein: Start Orchestrator`
- `Bernstein: Stop (soft)`
- `Bernstein: Stop (hard)`
- `Bernstein: Spawn Agent`
- `Bernstein: Show Dashboard`

**Chat Participant** (VS Code 1.109+):
```
@bernstein status
@bernstein spawn backend "Add rate limiting"
@bernstein costs
```

**Agent Skill** contribution — VS Code's built-in agents can query Bernstein.

**Real-time updates** via SSE from `/events` endpoint (already exists in Bernstein).

### Bernstein API endpoints consumed

| Feature | Endpoint |
|---------|----------|
| Dashboard data | GET `/dashboard/data` |
| Task list | GET `/tasks` |
| Agent logs | GET `/agents/{id}/stream` (SSE) |
| Kill agent | POST `/agents/{id}/kill` |
| Costs | GET `/costs/live` |
| Events | GET `/events` (SSE) |
| Start/stop | POST `/shutdown` |

### Publishing

**VS Code Marketplace:**
- Create publisher at marketplace.visualstudio.com/manage
- `npm install -g @vscode/vsce && vsce publish`
- Required: icon (128x128), README, CHANGELOG, LICENSE

**Open VSX** (for Cursor, VSCodium, Gitpod):
- `npm install -g ovsx && ovsx publish -p <token>`
- Cursor uses Open VSX — this is how Cursor users get it

**CI/CD:** GitHub Action `HaaLeo/publish-vscode-extension@v1` publishes to both on tag push.

**Google Antigravity:** Uses VS Code extensions — same VSIX works.

### Build tooling

- esbuild for extension (Node.js target)
- Vite for webview React app (browser target)
- Two separate builds, one `vsce package` step

## Files to create

- `packages/vscode/` — entire extension package
- `.github/workflows/publish-extension.yml` — CI/CD for marketplace

## Completion signal

- Extension installs from VS Code Marketplace
- Shows agent/task status in sidebar
- Live cost tracking in status bar
- SSE-powered real-time updates
- Works in VS Code, Cursor, and Antigravity
