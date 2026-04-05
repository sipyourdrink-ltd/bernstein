import * as vscode from 'vscode';
import type { DashboardData, BernsteinAgent } from './BernsteinClient';

function getNonce(): string {
  const chars =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from(
    { length: 32 },
    () => chars[Math.floor(Math.random() * chars.length)],
  ).join('');
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Build a 100x24 SVG sparkline from a rolling cost history array. */
function buildSparkline(values: number[]): string {
  if (values.length < 2) return '';
  const W = 100, H = 24;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 0.001;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - ((v - min) / range) * (H - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" `
    + `class="sparkline" aria-hidden="true">`
    + `<polyline points="${points}" fill="none" stroke="currentColor" `
    + `stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    + `</svg>`;
}

/** Build task status breakdown pills for the Tasks stat card. */
function buildTaskBreakdown(stats: DashboardData['stats']): string {
  const pills: string[] = [];
  if (stats.open > 0) {
    pills.push(`<span class="pill pill-open">${stats.open} open</span>`);
  }
  const inProgress = stats.claimed;
  if (inProgress > 0) {
    pills.push(`<span class="pill pill-progress">${inProgress} in progress</span>`);
  }
  if (stats.done > 0) {
    pills.push(`<span class="pill pill-done">${stats.done} done</span>`);
  }
  if (stats.failed > 0) {
    pills.push(`<span class="pill pill-failed">${stats.failed} failed</span>`);
  }
  if (pills.length === 0) return '';
  return `<div class="pill-row">${pills.join('')}</div>`;
}

/** Build agent cards for active agents only. */
function buildAgentCards(agents: BernsteinAgent[]): string {
  const active = agents.filter(
    (a) => a.status === 'working' || a.status === 'starting',
  );
  if (!active.length) return '';

  const cards = active.map((agent) => {
    const runtime =
      agent.runtime_s > 60
        ? `${Math.floor(agent.runtime_s / 60)}m`
        : `${agent.runtime_s}s`;
    const cost = `$${agent.cost_usd.toFixed(2)}`;
    const model = escapeHtml(agent.model ?? agent.role);
    const tasks = agent.tasks ?? [];
    const currentTask =
      tasks.find((t) => t.status === 'in_progress') ?? tasks[0];
    const progress = Math.min(100, Math.max(0, currentTask?.progress ?? 0));
    const agentId = escapeHtml(agent.id.slice(0, 14));

    return `<div class="agent-card">
  <div class="agent-row">
    <span class="dot-active">\u25CF</span>
    <span class="agent-id">${agentId}</span>
    <span class="agent-meta">${model} \u00B7 ${runtime} \u00B7 ${cost}</span>
  </div>${
    currentTask
      ? `
  <div class="task-name">${escapeHtml(currentTask.title.slice(0, 52))}</div>
  <div class="progress-track"><div class="progress-fill" style="width:${progress}%"></div></div>`
      : ''
  }
</div>`;
  }).join('');

  return `<h2>Agents</h2>${cards}`;
}

/** 4 skeleton placeholder cards shown while data is loading. */
function buildSkeletonGrid(): string {
  return `<div class="stats-grid">
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
    <div class="skeleton stat-card"></div>
  </div>`;
}

/** Format seconds-ago into a human-readable string. */
function formatTimeAgo(secondsAgo: number): string {
  if (secondsAgo < 5) return 'just now';
  if (secondsAgo < 60) return `${Math.floor(secondsAgo)}s ago`;
  if (secondsAgo < 3600) return `${Math.floor(secondsAgo / 60)}m ago`;
  return `${Math.floor(secondsAgo / 3600)}h ago`;
}

/** Build the "Last updated" footer line. */
function buildLastUpdated(ts: number, nowTs: number): string {
  const secondsAgo = Math.max(0, nowTs - ts);
  return `<div class="last-updated">Last updated: ${formatTimeAgo(secondsAgo)}</div>`;
}

/** Build the empty state when the orchestrator is not running. */
function buildOfflineState(baseUrl: string): string {
  return `<div class="empty-state">
  <div class="empty-icon">\uD83C\uDFBC</div>
  <div class="empty-title">Bernstein is not running</div>
  <div class="empty-subtitle">Trying to connect to<br><code>${escapeHtml(baseUrl)}</code></div>
  <button class="empty-btn" id="startBtn">Start Bernstein</button>
</div>`;
}

/** Build the empty state when connected but no agents/tasks. */
function buildIdleState(): string {
  return `<div class="empty-state">
  <div class="empty-icon">\uD83C\uDFBC</div>
  <div class="empty-title">No active agents</div>
  <div class="empty-subtitle">Run <code>bernstein run</code> to start orchestrating.</div>
</div>`;
}

export interface BuildHtmlOptions {
  data: DashboardData | null;
  costHistory: number[];
  baseUrl: string;
  nowTs: number;
  isRefreshing: boolean;
}

export function buildHtml(options: BuildHtmlOptions): string {
  const { data, costHistory, baseUrl, nowTs, isRefreshing } = options;
  const nonce = getNonce();
  const csp = `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';`;

  const stats = data?.stats;
  const total = stats ? stats.total : 0;
  const successRate = total > 0 ? Math.round((stats!.done / total) * 100) : 0;
  const sparkline = buildSparkline(costHistory);

  const refreshDotHtml = `<div class="refresh-dot${isRefreshing ? ' refreshing' : ''}" title="${isRefreshing ? 'Refreshing...' : 'Connected'}"></div>`;

  const statsHtml = stats
    ? `
      <div class="stat-card">
        <div class="stat-value">${stats.agents}</div>
        <div class="stat-label">Agents</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${stats.done}/${total}</div>
        <div class="stat-label">Tasks</div>
        ${buildTaskBreakdown(stats)}
      </div>
      <div class="stat-card">
        <div class="stat-value">${successRate}%</div>
        <div class="stat-label">Success</div>
      </div>
      <div class="stat-card cost-card">
        <div class="stat-value">$${stats.cost_usd.toFixed(2)}</div>
        <div class="stat-label">Cost${sparkline ? ` <span class="sparkline-wrap">${sparkline}</span>` : ''}</div>
      </div>`
    : null;

  const agentCardsHtml = data ? buildAgentCards(data.agents) : '';

  const alertsHtml = (data?.alerts ?? [])
    .map(
      (a) =>
        `<div class="alert alert-${escapeHtml(a.level)}">${escapeHtml(a.message)}</div>`,
    )
    .join('');

  // Determine body content based on state
  let bodyContent: string;
  if (!data) {
    // Not connected
    bodyContent = `${buildSkeletonGrid()}${buildOfflineState(baseUrl)}`;
  } else if (stats && stats.agents === 0 && total === 0) {
    // Connected but idle
    bodyContent = `<div class="header-bar"><h2>Overview</h2>${refreshDotHtml}</div>
<div class="stats-grid">${statsHtml}</div>
${buildIdleState()}
${data.ts ? buildLastUpdated(data.ts, nowTs) : ''}`;
  } else {
    // Active state
    bodyContent = `<div class="header-bar"><h2>Overview</h2>${refreshDotHtml}</div>
<div class="stats-grid">${statsHtml}</div>
${agentCardsHtml}
${alertsHtml ? `<h2>Alerts</h2><div class="alerts">${alertsHtml}</div>` : ''}
${data.ts ? buildLastUpdated(data.ts, nowTs) : ''}`;
  }

  const script = `<script nonce="${nonce}">
(function() {
  const vscode = acquireVsCodeApi();
  const btn = document.getElementById('startBtn');
  if (btn) {
    btn.addEventListener('click', function() {
      vscode.postMessage({ command: 'bernstein.start' });
    });
  }
})();
</script>`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Bernstein</title>
  <style nonce="${nonce}">
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 13px;
      line-height: 1.4;
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      padding: 12px;
    }
    h2 {
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: var(--vscode-descriptionForeground);
      margin: 14px 0 6px;
    }
    h2:first-child { margin-top: 0; }
    .header-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .header-bar h2 { margin-top: 0; flex: 1; }
    .refresh-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--vscode-charts-green, #73c48f);
      opacity: 0.5;
      flex-shrink: 0;
    }
    .refresh-dot.refreshing {
      animation: pulse 0.8s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 0.3; transform: scale(1); }
      50% { opacity: 1; transform: scale(1.4); }
    }
    .stats-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .stat-card {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      padding: 10px 10px 8px;
    }
    .stat-value {
      font-size: 18px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      margin-bottom: 3px;
    }
    .stat-label {
      font-size: 10px;
      color: var(--vscode-descriptionForeground);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 3px;
      margin-top: 5px;
    }
    .pill {
      display: inline-block;
      font-size: 9px;
      font-weight: 500;
      padding: 1px 5px;
      border-radius: 8px;
      line-height: 1.4;
      white-space: nowrap;
    }
    .pill-open {
      color: var(--vscode-descriptionForeground);
      border: 1px solid var(--vscode-descriptionForeground);
      opacity: 0.8;
    }
    .pill-progress {
      color: var(--vscode-charts-blue, #5794f2);
      border: 1px solid var(--vscode-charts-blue, #5794f2);
    }
    .pill-done {
      color: var(--vscode-charts-green, #73c48f);
      border: 1px solid var(--vscode-charts-green, #73c48f);
    }
    .pill-failed {
      color: var(--vscode-charts-red, #f2495c);
      border: 1px solid var(--vscode-charts-red, #f2495c);
    }
    .sparkline-wrap {
      display: flex;
      align-items: center;
      margin-left: auto;
    }
    .sparkline {
      color: var(--vscode-charts-blue, #5794f2);
      display: block;
    }
    .agent-card {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      padding: 8px 10px;
      margin-bottom: 4px;
    }
    .agent-row {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 3px;
    }
    .dot-active {
      font-size: 8px;
      color: var(--vscode-charts-green, #73c48f);
      flex-shrink: 0;
    }
    .agent-id {
      font-weight: 500;
      font-variant-numeric: tabular-nums;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .agent-meta {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .task-name {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      margin-bottom: 5px;
      padding-left: 14px;
    }
    .progress-track {
      height: 2px;
      background: var(--vscode-editorWidget-border, rgba(128,128,128,0.15));
      border-radius: 1px;
      overflow: hidden;
    }
    .progress-fill {
      height: 100%;
      background: var(--vscode-charts-blue, #5794f2);
      border-radius: 1px;
      min-width: 2%;
    }
    .alerts { display: flex; flex-direction: column; gap: 6px; }
    .alert {
      padding: 8px 10px;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.4;
    }
    .alert-warning {
      background: var(--vscode-inputValidation-warningBackground);
      border: 1px solid var(--vscode-inputValidation-warningBorder);
    }
    .alert-error {
      background: var(--vscode-inputValidation-errorBackground);
      border: 1px solid var(--vscode-inputValidation-errorBorder);
    }
    .alert-info {
      background: var(--vscode-editor-background);
      opacity: 0.8;
    }
    .empty-state {
      text-align: center;
      padding: 24px 8px 16px;
    }
    .empty-icon {
      font-size: 32px;
      margin-bottom: 10px;
    }
    .empty-title {
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 6px;
    }
    .empty-subtitle {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      margin-bottom: 14px;
      line-height: 1.5;
    }
    .empty-subtitle code {
      font-size: 10px;
      padding: 1px 4px;
      border-radius: 3px;
      background: var(--vscode-editor-background);
    }
    .empty-btn {
      display: inline-block;
      padding: 5px 14px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      border: none;
      border-radius: 3px;
      color: var(--vscode-button-foreground);
      background: var(--vscode-button-background);
    }
    .empty-btn:hover {
      background: var(--vscode-button-hoverBackground);
    }
    .last-updated {
      font-size: 10px;
      color: var(--vscode-disabledForeground);
      text-align: right;
      margin-top: 12px;
      padding-top: 6px;
      border-top: 1px solid var(--vscode-editorWidget-border, rgba(128,128,128,0.1));
    }
    @keyframes shimmer {
      0%, 100% { opacity: 0.35; }
      50% { opacity: 0.65; }
    }
    .skeleton {
      background: var(--vscode-editor-background);
      border-radius: 5px;
      animation: shimmer 1.5s ease-in-out infinite;
      height: 52px;
    }
  </style>
</head>
<body>
  ${bodyContent}
  ${script}
</body>
</html>`;
}

export class DashboardProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private data: DashboardData | null = null;
  private readonly costHistory: number[] = [];
  private isRefreshing = false;
  private baseUrl = 'http://127.0.0.1:8052';

  setBaseUrl(url: string): void {
    this.baseUrl = url;
  }

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = buildHtml({
      data: this.data,
      costHistory: this.costHistory,
      baseUrl: this.baseUrl,
      nowTs: Math.floor(Date.now() / 1000),
      isRefreshing: false,
    });

    webviewView.webview.onDidReceiveMessage((message: { command?: string }) => {
      if (message.command === 'bernstein.start') {
        void vscode.commands.executeCommand('bernstein.start');
      }
    });
  }

  /** Signal that a refresh is in progress (shows pulsing dot). */
  setRefreshing(value: boolean): void {
    this.isRefreshing = value;
    this.render();
  }

  update(data: DashboardData): void {
    this.data = data;
    this.isRefreshing = false;
    this.costHistory.push(data.stats.cost_usd);
    if (this.costHistory.length > 20) this.costHistory.shift();
    this.render();
  }

  private render(): void {
    if (this.view) {
      this.view.webview.html = buildHtml({
        data: this.data,
        costHistory: this.costHistory,
        baseUrl: this.baseUrl,
        nowTs: Math.floor(Date.now() / 1000),
        isRefreshing: this.isRefreshing,
      });
    }
  }

  /**
   * Opens the full Bernstein dashboard in the default browser.
   * VS Code webviews cannot iframe localhost due to CSP restrictions,
   * so we open externally.
   */
  static openInBrowser(baseUrl: string): void {
    void vscode.env.openExternal(vscode.Uri.parse(`${baseUrl}/dashboard`));
  }
}
