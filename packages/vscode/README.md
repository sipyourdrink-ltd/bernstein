# Bernstein — Multi-Agent Orchestration for VS Code

VS Code extension for monitoring and controlling [Bernstein](https://github.com/chernistry/bernstein), a multi-agent orchestration system that runs parallel AI coding agents. Gives you real-time visibility into tasks, agents, and costs without leaving your editor.

![Dashboard](media/screenshot-dashboard.png)

## Features

- **Sidebar views** — dedicated activity bar panel with Agents, Tasks, and Overview views
- **Agent tree with delegation hierarchy** — see active agents, their roles, models, and parent/child relationships
- **Task grouping** — tasks organized by status (open, in progress, done, failed) with inline actions
- **Dashboard webview** — at-a-glance stats: task breakdown, agent count, cost totals, alerts
- **SSE real-time updates** — live streaming from the orchestrator, no manual refresh needed
- **`@bernstein` chat participant** — query orchestrator status and costs from VS Code Chat
- **Cost tracking** — per-agent and per-model spend, budget warnings when thresholds are exceeded
- **Status bar** — persistent summary of agents, task progress, and total cost

## Commands

| Command | Description |
|---|---|
| `Bernstein: Start` | Launch the orchestrator in a terminal |
| `Bernstein: Refresh` | Force-refresh all tree views |
| `Bernstein: Show Dashboard` | Open the dashboard in a browser |
| `Bernstein: Kill Agent` | Terminate a running agent (context menu) |
| `Bernstein: Show Agent Output` | View an agent's execution logs (context menu) |
| `Bernstein: Cancel Task` | Cancel an open or in-progress task (context menu) |
| `Bernstein: Prioritize Task` | Move a task to the top of the queue (context menu) |
| `Bernstein: Open Task Output` | Open output for a task's assigned agent |
| `Bernstein: Inspect Agent` | Show agent details: role, model, runtime, cost, tasks |

## Configuration

| Setting | Type | Default | Description |
|---|---|---|---|
| `bernstein.apiUrl` | string | `http://127.0.0.1:8052` | Bernstein orchestrator API URL |
| `bernstein.apiToken` | string | `""` | Bearer token for API authentication (optional) |
| `bernstein.refreshInterval` | number | `5` | Tree view polling interval in seconds |
| `bernstein.showNotifications` | boolean | `true` | Show notification toasts for task completions and failures |
| `bernstein.costWarningThreshold` | number | `80` | Show a cost warning when budget usage exceeds this percentage |
| `bernstein.autoStart` | boolean | `false` | Automatically start Bernstein when opening a workspace with a `.sdd` directory |

## Requirements

- VS Code 1.100+ (or compatible forks: Cursor, VSCodium)
- Bernstein orchestrator running locally — install from [github.com/chernistry/bernstein](https://github.com/chernistry/bernstein)

## Quick Start

1. Install the extension from the VS Code Marketplace (or Open VSX)
2. Start the orchestrator: run `bernstein run` in your project directory
3. The extension auto-connects to `localhost:8052` and starts streaming updates
4. Click the Bernstein icon in the activity bar to see agents, tasks, and the dashboard

## Privacy

The extension only communicates with your local Bernstein server. No telemetry, no external network calls.

## License

Apache-2.0
